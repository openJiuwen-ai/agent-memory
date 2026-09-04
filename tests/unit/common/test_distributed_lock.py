"""分布式锁的契约与两个实现的行为校验。

契约是异步的，而本仓未装 pytest-asyncio，故沿用既有约定：同步用例内 ``asyncio.run``。

Redis 实现用假客户端校验**调用装配**（``SET NX PX`` 的参数、CAS 脚本的 keys/args），
Lua 语义由假客户端模拟而非真实执行——假客户端不能替代真 Redis 验证脚本的原子性，
这部分留给集成测试。互斥、租约、重入、看门狗等行为语义在 ``memory`` 实现上验证，
两个实现共用同一套 ``LockProvider`` 模板逻辑。
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

import jiuwen_memory.common.lock.lock_impl  # noqa: F401  触发自注册
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.lock import (
    KEY_PREFIX,
    LockHandle,
    LockProducer,
    LockTimeoutError,
)
from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
from jiuwen_memory.common.lock.lock_impl.redis_lock import _RELEASE_LUA, RedisLockProvider
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import AssemblyContext

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="prod", user="u1")


def _provider(*, lease_ms: int = 30_000, wait_timeout_ms: int = 0) -> InMemoryLockProvider:
    return InMemoryLockProvider(lease_ms=lease_ms, wait_timeout_ms=wait_timeout_ms)


async def _acquire_elsewhere(
    provider: InMemoryLockProvider,
    scope: Scope = _SCOPE,
    name: str = "write",
    *,
    wait_timeout_ms: int = 0,
) -> LockHandle:
    """从另一个 task 获取。

    同 task 内重复获取一定被判为重入（这是契约），故凡是要验证互斥的用例都必须换 task
    竞争，否则测到的是重入路径而非互斥路径。
    """
    return await asyncio.create_task(
        provider.acquire(scope, name, wait_timeout_ms=wait_timeout_ms)
    )


async def _assert_held(
    provider: InMemoryLockProvider, scope: Scope = _SCOPE, name: str = "write"
) -> None:
    with pytest.raises(LockTimeoutError):
        await _acquire_elsewhere(provider, scope, name)


# -- 注册与装配 ------------------------------------------------------------------ #


def test_both_implementations_registered() -> None:
    assert LockProducer.known() == ["memory", "redis"]


def test_top_name_is_a_known_config_section() -> None:
    from jiuwen_memory.common.factory.factory import Factory

    assert "lock" in Factory.known_top_names()


def test_memory_builder_applies_lease_and_timeout() -> None:
    provider = LockProducer.build(
        "memory", {"lease_ms": 50, "wait_timeout_ms": 30}, AssemblyContext()
    )
    assert isinstance(provider, InMemoryLockProvider)

    async def scenario() -> None:
        await provider.acquire(_SCOPE, "write")
        await asyncio.sleep(0.08)
        fresh = await _acquire_elsewhere(provider)
        await provider.release(fresh)

        await provider.acquire(_SCOPE, "write")

        started = time.monotonic()
        with pytest.raises(LockTimeoutError):
            await asyncio.create_task(provider.acquire(_SCOPE, "write"))
        assert 0.02 <= time.monotonic() - started < 0.5

    asyncio.run(scenario())


# -- 锁键 ------------------------------------------------------------------------ #


def test_key_renders_five_segments_with_placeholder() -> None:
    key = InMemoryLockProvider.build_key(_SCOPE, "write")
    assert key == f"{KEY_PREFIX}:acme:prod:u1:_:_:write"


def test_key_prefix_separates_locks_from_kv_namespace() -> None:
    """KV 数据键是裸的五段命名空间，锁必须带前缀，否则共用一个库会撞键。"""
    assert InMemoryLockProvider.build_key(_SCOPE, "write").startswith(f"{KEY_PREFIX}:")


def test_key_sanitizes_separators_in_scope_values() -> None:
    key = InMemoryLockProvider.build_key(Scope(org="a/b", user="c:d"), "write")
    assert key == f"{KEY_PREFIX}:a_b:_:c_d:_:_:write"


def test_key_granularity_is_the_callers_choice() -> None:
    """同一用户下，scope 收窄或 name 变化都得到不同的键。"""
    user_level = InMemoryLockProvider.build_key(_SCOPE, "write")
    session_level = InMemoryLockProvider.build_key(
        Scope(org="acme", space="prod", user="u1", session="s1"), "write"
    )
    other_purpose = InMemoryLockProvider.build_key(_SCOPE, "evolve")
    assert len({user_level, session_level, other_purpose}) == 3


@pytest.mark.parametrize("name", ["", "   ", None])
def test_empty_name_rejected(name) -> None:
    with pytest.raises(ValidationError):
        InMemoryLockProvider.build_key(_SCOPE, name)


# -- 互斥与有界等待 --------------------------------------------------------------- #


def test_second_acquire_blocks_until_release() -> None:
    provider = _provider()

    async def scenario() -> list[str]:
        order: list[str] = []
        first = await provider.acquire(_SCOPE, "write")

        async def contender() -> None:
            handle = await provider.acquire(_SCOPE, "write", wait_timeout_ms=2_000)
            order.append("second-acquired")
            await provider.release(handle)

        task = asyncio.create_task(contender())
        await asyncio.sleep(0.05)
        order.append("first-releasing")
        await provider.release(first)
        await task
        return order

    assert asyncio.run(scenario()) == ["first-releasing", "second-acquired"]


def test_wait_timeout_zero_tries_once_then_raises() -> None:
    provider = _provider()

    async def scenario() -> None:
        await provider.acquire(_SCOPE, "write")
        started = time.monotonic()
        with pytest.raises(LockTimeoutError):
            await _acquire_elsewhere(provider)
        assert time.monotonic() - started < 0.05

    asyncio.run(scenario())


def test_bounded_wait_gives_up_instead_of_spinning_forever() -> None:
    provider = _provider()

    async def scenario() -> None:
        await provider.acquire(_SCOPE, "write")
        started = time.monotonic()
        with pytest.raises(LockTimeoutError):
            await _acquire_elsewhere(provider, wait_timeout_ms=100)
        elapsed = time.monotonic() - started
        assert 0.09 <= elapsed < 1.0

    asyncio.run(scenario())


def test_different_keys_do_not_contend() -> None:
    provider = _provider()

    async def scenario() -> None:
        await provider.acquire(_SCOPE, "write")
        other_user = await provider.acquire(Scope(org="acme", space="prod", user="u2"), "write")
        other_name = await provider.acquire(_SCOPE, "evolve")
        assert other_user.token != other_name.token

    asyncio.run(scenario())


# -- 租约 ------------------------------------------------------------------------ #


def test_lease_expiry_lets_another_holder_in() -> None:
    provider = _provider(lease_ms=50)

    async def scenario() -> None:
        first = await provider.acquire(_SCOPE, "write")
        await asyncio.sleep(0.08)
        second = await _acquire_elsewhere(provider)
        assert second.token != first.token

    asyncio.run(scenario())


def test_stale_release_does_not_free_the_new_holders_lock() -> None:
    """租约过期后他人拿到同名锁，原持有者的 release 必须是空操作。"""
    provider = _provider(lease_ms=50)

    async def scenario() -> None:
        stale = await provider.acquire(_SCOPE, "write")
        await asyncio.sleep(0.08)
        fresh = await _acquire_elsewhere(provider)

        await provider.release(stale)

        await _assert_held(provider)
        await provider.release(fresh)
        await _acquire_elsewhere(provider)

    asyncio.run(scenario())


def test_renew_extends_lease_and_fails_after_loss() -> None:
    provider = _provider(lease_ms=60)

    async def scenario() -> None:
        handle = await provider.acquire(_SCOPE, "write")
        await asyncio.sleep(0.04)
        assert await provider.renew(handle) is True
        await asyncio.sleep(0.04)
        await _assert_held(provider)  # 续期后仍在租约内

        await asyncio.sleep(0.08)  # 续期后的租约也到期
        stolen = await _acquire_elsewhere(provider)
        assert stolen.token != handle.token
        assert await provider.renew(handle) is False

    asyncio.run(scenario())


# -- 重入 ------------------------------------------------------------------------ #


def test_same_task_reentry_returns_the_same_token() -> None:
    provider = _provider()

    async def scenario() -> None:
        outer = await provider.acquire(_SCOPE, "write")
        inner = await provider.acquire(_SCOPE, "write")
        assert inner.reentrant is True
        assert inner.token == outer.token

        await provider.release(inner)
        await _assert_held(provider)  # 内层释放只递减计数，不解锁

        await provider.release(outer)
        await _acquire_elsewhere(provider)

    asyncio.run(scenario())


def test_child_task_is_not_treated_as_reentry() -> None:
    """重入以 asyncio.Task 为身份边界：create_task 派生的子任务正常参与竞争。"""
    provider = _provider()

    async def scenario() -> None:
        await provider.acquire(_SCOPE, "write")

        async def child() -> None:
            with pytest.raises(LockTimeoutError):
                await provider.acquire(_SCOPE, "write", wait_timeout_ms=0)

        await asyncio.create_task(child())

    asyncio.run(scenario())


def test_nested_guard_starts_only_one_renewer() -> None:
    provider = _provider(lease_ms=30_000)

    async def scenario() -> None:
        before = len(asyncio.all_tasks())
        async with provider.guard(_SCOPE, "write"):
            outer_renewers = len(asyncio.all_tasks()) - before
            async with provider.guard(_SCOPE, "write") as inner:
                assert inner.reentrant is True
                assert len(asyncio.all_tasks()) - before == outer_renewers
        assert outer_renewers == 1

    asyncio.run(scenario())


# -- guard 与看门狗 --------------------------------------------------------------- #


def test_guard_releases_on_exception() -> None:
    provider = _provider()

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with provider.guard(_SCOPE, "write"):
                raise RuntimeError("boom")
        await provider.acquire(_SCOPE, "write", wait_timeout_ms=0)

    asyncio.run(scenario())


def test_watchdog_keeps_the_lock_past_its_lease() -> None:
    provider = _provider(lease_ms=90)

    async def scenario() -> None:
        async with provider.guard(_SCOPE, "write") as handle:
            await asyncio.sleep(0.25)  # 远超一个租约周期
            assert handle.lost.is_set() is False
            await _assert_held(provider)

    asyncio.run(scenario())


class _RenewFailingLockProvider(InMemoryLockProvider):
    """模拟后端在看门狗续租时报告已失去持有权。"""

    async def renew(self, handle: LockHandle, *, lease_ms: int | None = None) -> bool:
        return False


def test_watchdog_signals_loss_instead_of_silently_continuing() -> None:
    provider = _RenewFailingLockProvider(lease_ms=90, wait_timeout_ms=0)

    async def scenario() -> None:
        async with provider.guard(_SCOPE, "write") as handle:
            await asyncio.wait_for(handle.lost.wait(), timeout=1.0)
            assert handle.lost.is_set() is True

    asyncio.run(scenario())


def test_guard_without_auto_renew_starts_no_task() -> None:
    provider = _provider()

    async def scenario() -> None:
        before = len(asyncio.all_tasks())
        async with provider.guard(_SCOPE, "write", auto_renew=False):
            assert len(asyncio.all_tasks()) == before

    asyncio.run(scenario())


# -- Redis 实现：调用装配 ---------------------------------------------------------- #


class _FakeScript:
    """模拟 redis-py 的 Script 对象；按脚本源码分派到对应的 CAS 语义。"""

    def __init__(self, backing: "_FakeRedis", source: str) -> None:
        self._backing = backing
        self._source = source

    async def __call__(self, keys, args, client=None):
        self._backing.calls.append((self._source, list(keys), list(args)))
        key = keys[0]
        if self._backing.store.get(key) != args[0]:
            return 0
        if "DEL" in self._source:
            self._backing.store.pop(key, None)
            return 1
        self._backing.expiries[key] = int(args[1])
        return 1


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.set_calls: list[dict] = []
        self.pinged = False
        self.connection_url: str | None = None
        self.connection_options: dict = {}

    async def set(self, key, value, nx=False, px=None):
        self.set_calls.append({"key": key, "value": value, "nx": nx, "px": px})
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.expiries[key] = px
        return True

    def register_script(self, source: str) -> _FakeScript:
        return _FakeScript(self, source)

    async def ping(self) -> bool:
        self.pinged = True
        return True


def _install_fake_redis(monkeypatch, fake: _FakeRedis) -> None:
    """在模块边界替换 redis.asyncio，保留 Provider 的惰性建连与脚本注册路径。"""

    class _RedisFactory:
        @staticmethod
        def from_url(url: str, **options) -> _FakeRedis:
            fake.connection_url = url
            fake.connection_options = options
            return fake

    redis_module = types.ModuleType("redis")
    redis_module.asyncio = types.SimpleNamespace(Redis=_RedisFactory)
    monkeypatch.setitem(sys.modules, "redis", redis_module)


def _redis_provider(monkeypatch, **kwargs) -> tuple[RedisLockProvider, _FakeRedis]:
    fake = _FakeRedis()
    _install_fake_redis(monkeypatch, fake)
    provider = RedisLockProvider(url="redis://localhost:6379/1", **kwargs)
    return provider, fake


def test_redis_acquire_uses_set_nx_px(monkeypatch) -> None:
    provider, fake = _redis_provider(monkeypatch, lease_ms=30_000)

    handle = asyncio.run(provider.acquire(_SCOPE, "write"))

    assert fake.set_calls == [
        {
            "key": f"{KEY_PREFIX}:acme:prod:u1:_:_:write",
            "value": handle.token,
            "nx": True,
            "px": 30_000,
        }
    ]


def test_redis_release_is_a_cas_on_the_token(monkeypatch) -> None:
    provider, fake = _redis_provider(monkeypatch)

    async def scenario() -> None:
        handle = await provider.acquire(_SCOPE, "write")
        await provider.release(handle)
        assert handle.key not in fake.store

        # 他人持有时，本方的释放不得删掉这把锁
        fake.store[handle.key] = "someone-else"
        await provider.release(handle)
        assert fake.store[handle.key] == "someone-else"

    asyncio.run(scenario())
    assert all(source is _RELEASE_LUA for source, _, _ in fake.calls)


def test_redis_renew_is_a_cas_and_reports_loss(monkeypatch) -> None:
    provider, fake = _redis_provider(monkeypatch, lease_ms=1_000)

    async def scenario() -> None:
        handle = await provider.acquire(_SCOPE, "write")
        assert await provider.renew(handle, lease_ms=5_000) is True
        assert fake.expiries[handle.key] == 5_000

        fake.store[handle.key] = "someone-else"
        assert await provider.renew(handle) is False

    asyncio.run(scenario())


def test_redis_health_pings(monkeypatch) -> None:
    provider, fake = _redis_provider(monkeypatch)
    asyncio.run(provider.health())
    assert fake.pinged is True


def test_redis_acquire_times_out_when_held(monkeypatch) -> None:
    provider, fake = _redis_provider(monkeypatch)
    fake.store[f"{KEY_PREFIX}:acme:prod:u1:_:_:write"] = "someone-else"

    with pytest.raises(LockTimeoutError):
        asyncio.run(provider.acquire(_SCOPE, "write", wait_timeout_ms=0))


def test_redis_connect_failure_is_a_backend_error() -> None:
    """fail-closed：建连失败一律抛错，不静默降级为无锁。"""
    provider = RedisLockProvider(url="not-a-url")
    handle = LockHandle(key="k", token="t", lease_ms=1)

    with pytest.raises(BackendError):
        asyncio.run(provider.release(handle))


# -- Redis builder：装配期 TLS 校验 ------------------------------------------------ #


def _build_redis(params: dict):
    return LockProducer.build("redis", params, AssemblyContext())


def test_redis_builder_requires_url() -> None:
    with pytest.raises(ValidationError):
        _build_redis({})


def test_redis_builder_defaults_lease_and_timeout(monkeypatch) -> None:
    provider = _build_redis({"url": "redis://localhost:6379/1"})
    fake = _FakeRedis()
    _install_fake_redis(monkeypatch, fake)

    asyncio.run(provider.acquire(_SCOPE, "write", wait_timeout_ms=0))
    assert fake.set_calls[0]["px"] == 30_000


def test_redis_builder_allows_plaintext_when_verify_off(monkeypatch) -> None:
    provider = _build_redis({"url": "redis://localhost:6379/1", "ssl_verify": "false"})
    fake = _FakeRedis()
    _install_fake_redis(monkeypatch, fake)

    assert provider.client is fake
    assert fake.connection_options == {"decode_responses": True}


def test_redis_builder_requires_ca_cert_when_verify_on() -> None:
    with pytest.raises(ValidationError):
        _build_redis({"url": "rediss://localhost:6379/1", "ssl_verify": "true"})


def test_redis_builder_rejects_plaintext_scheme_when_verify_on(tmp_path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    with pytest.raises(ValidationError):
        _build_redis(
            {
                "url": "redis://localhost:6379/1",
                "ssl_verify": "true",
                "ssl_ca_cert": str(ca),
            }
        )


def test_redis_builder_rejects_tls_query_params_when_verify_on(tmp_path) -> None:
    """URL query 会覆盖 kwargs 并可能悄悄关掉校验，必须拦在装配期。"""
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    with pytest.raises(ValidationError):
        _build_redis(
            {
                "url": "rediss://localhost:6379/1?ssl_cert_reqs=none",
                "ssl_verify": "true",
                "ssl_ca_cert": str(ca),
            }
        )


def test_redis_builder_passes_ca_cert_when_verify_on(monkeypatch, tmp_path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    provider = _build_redis(
        {
            "url": "rediss://localhost:6379/1",
            "ssl_verify": "true",
            "ssl_ca_cert": str(ca),
        }
    )
    fake = _FakeRedis()
    _install_fake_redis(monkeypatch, fake)

    assert provider.client is fake
    assert fake.connection_options == {
        "decode_responses": True,
        "ssl_ca_certs": str(ca),
    }
