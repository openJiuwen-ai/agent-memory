"""bootstrap/core/auth_middleware：凭据归一 + ContextVar 生命周期。

中间件本身不含认证策略（模式由配置在装配期选定），故这里测的只有两件事：
**凭据材料被正确归一**，以及**上下文一定被清理**。后者是 05 最容易出的 bug，
测法是验证行为后果（``get_current()`` 是否干净），不是验证 ``reset_current``
被调用过。
"""

from __future__ import annotations

import os
import sys

import pytest

# bootstrap/core 是 flat import root（不是包），与各 surface 用同样的方式接进来。
_CORE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "bootstrap", "core")
)
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

from auth_middleware import authenticated, credentials_from_headers  # noqa: E402

from common.errors import AuthenticationError, RateLimitedError  # noqa: E402
from common.type_def.auth import Role, get_current  # noqa: E402
from common.type_def.scope import Scope  # noqa: E402
from config.context import AssemblyContext  # noqa: E402
from security.authenticator_impl.api_key_authenticator import ApiKeyAuthenticator  # noqa: E402
from security.authenticator_impl.dev_authenticator import DevAuthenticator  # noqa: E402
from security.authenticator_impl.trusted_authenticator import TrustedAuthenticator  # noqa: E402
from security.bootstrap import register_security  # noqa: E402
from security.key_store import KeyStoreProducer  # noqa: E402
from security.types import Credentials  # noqa: E402

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", user="alice")


@pytest.fixture(scope="module")
def key_store():
    register_security()
    return KeyStoreProducer.build("memory", {}, AssemblyContext())


@pytest.fixture(scope="module")
def alice_key(key_store) -> str:
    return key_store.issue(_ALICE, Role.USER)


# -- 凭据归一 ---------------------------------------------------------------- #


def test_bearer_scheme_is_case_insensitive() -> None:
    """RFC 9110 §11.1：auth-scheme 大小写不敏感。三种写法必须取到同一个 key。"""
    for raw in ("Bearer k123", "bearer k123", "BEARER k123"):
        assert credentials_from_headers({"Authorization": raw}).api_key == "k123"


def test_header_names_are_normalized_to_lowercase() -> None:
    """header 名大小写不敏感（RFC 9110 §5.1）——TRUSTED 实现按小写常量查。

    归一放在这里而不是各 authenticator 里，是为了让「查 header」只有一种写法。
    """
    creds = credentials_from_headers({"X-ORG-Id": "acme", "x-Principal-TYPE": "user"})
    assert creds.headers == {"x-org-id": "acme", "x-principal-type": "user"}


def test_x_api_key_is_the_fallback_not_the_override(alice_key) -> None:
    """Authorization 优先；它缺失或非 Bearer 时才回落 X-Api-Key。

    顺序反过来会让「同时带两个 header」的请求用哪个 key 取决于实现细节。
    """
    both = credentials_from_headers({"Authorization": "Bearer from-bearer", "X-Api-Key": "from-x"})
    assert both.api_key == "from-bearer"

    only_x = credentials_from_headers({"X-Api-Key": "from-x"})
    assert only_x.api_key == "from-x"

    # Basic 不是 Bearer，不该被当成 api_key 提取，此时回落 X-Api-Key。
    basic = credentials_from_headers({"Authorization": "Basic dXNlcjpwdw==", "X-Api-Key": "from-x"})
    assert basic.api_key == "from-x"


def test_missing_credentials_yield_empty_key_not_none() -> None:
    """无凭据是空串而非 None——authenticator 侧不必再写 None 判断。"""
    creds = credentials_from_headers({})
    assert creds.api_key == ""
    assert creds.headers == {}
    assert creds.peer_address == ""


def test_peer_address_is_carried_through() -> None:
    """审计要记调用方地址（未来还要给速率限制用）。"""
    assert credentials_from_headers({}, "10.0.0.7").peer_address == "10.0.0.7"


def test_surrounding_whitespace_is_stripped() -> None:
    assert credentials_from_headers({"Authorization": "Bearer  k123 "}).api_key == "k123"
    assert credentials_from_headers({"X-Api-Key": " k123 "}).api_key == "k123"


# -- ContextVar 生命周期 ------------------------------------------------------ #


def test_context_is_set_inside_and_cleared_outside() -> None:
    assert get_current() is None
    with authenticated(DevAuthenticator(), Credentials()) as ctx:
        assert get_current() is ctx
        assert ctx.role is Role.ROOT
    assert get_current() is None


def test_context_is_cleared_when_body_raises() -> None:
    """with 体内抛异常同样要清理——否则一次 500 就污染整条线程。"""
    with pytest.raises(RuntimeError):
        with authenticated(DevAuthenticator(), Credentials()):
            raise RuntimeError("boom")
    assert get_current() is None


def test_failed_authentication_leaves_no_context(key_store) -> None:
    """认证失败后必须仍是「无身份」，不能残留上一次的。"""
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="")
    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="not-a-real-key")):
            pass  # pragma: no cover - authenticate 在进入 with 体之前就抛了
    assert get_current() is None


def test_consecutive_requests_do_not_inherit_identity(key_store, alice_key) -> None:
    """池化线程上连续两个请求：第二个必须看不到第一个的身份。

    这是漏 reset 的真实后果，也是本中间件存在的主要理由。
    """
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="")

    with authenticated(auth, Credentials(api_key=alice_key)) as ctx:
        assert ctx.actor == _ALICE
    assert get_current() is None

    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="wrong")):
            pass  # pragma: no cover
    assert get_current() is None


# -- 归一后的 header 确实能被 TRUSTED 消费 ------------------------------------- #


def test_normalized_headers_authenticate_under_trusted(key_store) -> None:
    """端到端一小段：大小写混乱的网关 header 经归一后仍认得出主体。

    单独测归一、单独测 TRUSTED 都会漏掉「两边约定不一致」这个真实故障。
    """
    auth = TrustedAuthenticator(key_store=key_store, gateway_key="")
    creds = credentials_from_headers(
        {"X-Org-ID": "acme", "X-Principal-Type": "User", "X-PRINCIPAL-ID": "alice"}
    )
    with authenticated(auth, creds) as ctx:
        assert ctx.actor == _ALICE
        assert ctx.role is Role.USER


# -- 限流（§8.1） ------------------------------------------------------------- #


class _CountingAuth(DevAuthenticator):
    """记下 authenticate 被调了几次——限流是否真的挡在认证之前，只能这样测。"""

    def __init__(self) -> None:
        self.calls = 0

    def authenticate(self, credentials):
        self.calls += 1
        return super().authenticate(credentials)


class _Blocked:
    """恒拒绝的限流器。"""

    @staticmethod
    def allow(peer):
        return False

    @staticmethod
    def health() -> None:
        return None


class _Open:
    @staticmethod
    def allow(peer):
        return True

    @staticmethod
    def health() -> None:
        return None


def _peer() -> Credentials:
    """带对端地址的凭据：限流按 peer 建桶，没有 peer 就不限流。"""
    return Credentials(peer_address="10.0.0.7")


def test_rate_limit_runs_before_authentication() -> None:
    """这是限流存在的全部理由：被限流的请求**不能**触发 Argon2 verify。

    放在认证之后限流，等于「先让攻击者把 CPU 用掉，再告诉他超限了」——
    §8.1 要防的资源耗尽就完全没防住。
    """
    auth = _CountingAuth()
    with pytest.raises(RateLimitedError):
        with authenticated(auth, _peer(), None, _Blocked()):
            pass  # pragma: no cover - 限流在进入 with 体之前就抛了
    assert auth.calls == 0


def test_rate_limited_is_not_an_authentication_error() -> None:
    """429 与 401 必须可分：一个该稍后重试，一个该换凭据。"""
    with pytest.raises(RateLimitedError) as exc:
        with authenticated(DevAuthenticator(), _peer(), None, _Blocked()):
            pass  # pragma: no cover
    assert not isinstance(exc.value, AuthenticationError)


def test_rate_limited_leaves_no_context() -> None:
    with pytest.raises(RateLimitedError):
        with authenticated(DevAuthenticator(), _peer(), None, _Blocked()):
            pass  # pragma: no cover
    assert get_current() is None


def test_no_limiter_means_no_limiting() -> None:
    """``limiter=None`` 是进程内直连 / MCP stdio 的形态，行为与一期一致。"""
    auth = _CountingAuth()
    for _ in range(5):
        with authenticated(auth, Credentials()):
            pass
    assert auth.calls == 5


def test_passing_limiter_does_not_change_the_allowed_path() -> None:
    with authenticated(DevAuthenticator(), _peer(), None, _Open()) as ctx:
        assert ctx.role is Role.ROOT
    assert get_current() is None


def test_rate_limit_denial_is_audited_distinctly() -> None:
    """审计里限流与认证失败要分得开，否则运维看到一堆 deny 不知道该调哪个。"""
    recorded = []

    class _Recorder:
        @staticmethod
        def record(event):
            recorded.append(event)

    with pytest.raises(RateLimitedError):
        with authenticated(
            DevAuthenticator(), Credentials(peer_address="10.0.0.7"), _Recorder(), _Blocked()
        ):
            pass  # pragma: no cover

    assert len(recorded) == 1
    assert recorded[0].action == "rate_limit"
    assert recorded[0].decision == "deny"
    assert recorded[0].actor == Scope()  # 身份未知，不可用调用方声明的值填充
    assert recorded[0].detail["peer"] == "10.0.0.7"


def test_rate_limit_audit_carries_no_bucket_state() -> None:
    """不记桶余量：那能用来反推限流参数，然后贴着阈值发请求。"""
    recorded = []

    class _Recorder:
        @staticmethod
        def record(event):
            recorded.append(event)

    with pytest.raises(RateLimitedError):
        with authenticated(
            DevAuthenticator(),
            Credentials(api_key="secret-key", peer_address="10.0.0.7"),
            _Recorder(),
            _Blocked(),
        ):
            pass  # pragma: no cover

    detail = recorded[0].detail
    assert set(detail) == {"auth_mode", "peer"}
    assert "secret-key" not in str(detail)  # §7.5：凭据不进审计


def test_audit_backend_failure_does_not_mask_429() -> None:
    """审计写失败不该把 429 变成 500——与 401 同样的取舍。"""

    class _Exploding:
        @staticmethod
        def record(event):
            raise RuntimeError("audit backend down")

    with pytest.raises(RateLimitedError):
        with authenticated(
            DevAuthenticator(), Credentials(peer_address="10.0.0.7"), _Exploding(), _Blocked()
        ):
            pass  # pragma: no cover


def test_audit_backend_failure_does_not_mask_401(key_store) -> None:
    """审计写失败不该把 401 变成 500——认证结论优先于可观测性。"""

    class _Exploding:
        @staticmethod
        def record(event):
            raise RuntimeError("audit backend down")

    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="")
    with pytest.raises(AuthenticationError):
        with authenticated(auth, Credentials(api_key="wrong"), _Exploding()):
            pass  # pragma: no cover
    assert get_current() is None


# -- Argon2 并发上限（审计 P1-3） ------------------------------------------- #


def test_argon2_guard_release_on_success() -> None:
    """guard 在认证成功后必须释放，否则槽位泄漏把后续请求也堵死。"""
    from security.concurrency_guard import Argon2Guard

    guard = Argon2Guard(max_concurrent=1)
    auth = _CountingAuth()
    with authenticated(auth, Credentials(), None, None, argon2_guard=guard):
        pass
    # 认证后槽位已释放，能再 acquire
    assert guard.acquire() is True
    guard.release()
    assert auth.calls == 1


def test_argon2_guard_release_on_auth_failure() -> None:
    """认证失败也要释放（finally）。"""
    from security.concurrency_guard import Argon2Guard

    guard = Argon2Guard(max_concurrent=1)

    # 用一个恒失败的 auth：走认证失败路径，验证 guard 在 finally 释放
    class _Fail:
        mode = DevAuthenticator().mode

        @staticmethod
        def authenticate(credentials):
            raise AuthenticationError("nope")

    with pytest.raises(AuthenticationError):
        with authenticated(_Fail(), Credentials(), None, None, argon2_guard=guard):
            pass  # pragma: no cover
    assert guard.acquire() is True
    guard.release()


def test_argon2_guard_blocks_when_slots_exhausted() -> None:
    """耗尽并发槽返回 429，不进入 authenticate。"""
    from security.concurrency_guard import Argon2Guard

    guard = Argon2Guard(max_concurrent=1)
    # 占满唯一槽位
    assert guard.acquire() is True
    auth = _CountingAuth()
    with pytest.raises(RateLimitedError):
        with authenticated(auth, Credentials(), None, None, argon2_guard=guard):
            pass  # pragma: no cover
    assert auth.calls == 0
    guard.release()


def test_argon2_guard_released_on_rate_limit_before_it() -> None:
    """IP 桶先挡住时 guard 不该 acquire（两层独立）。"""
    from security.concurrency_guard import Argon2Guard

    guard = Argon2Guard(max_concurrent=1)
    with pytest.raises(RateLimitedError):
        with authenticated(DevAuthenticator(), _peer(), None, _Blocked(), argon2_guard=guard):
            pass  # pragma: no cover
    # guard 没被占
    assert guard.acquire() is True
    guard.release()


def test_argon2_guard_none_means_unlimited() -> None:
    """None 表示不限（DEV / 进程内直连），与一期行为一致。"""
    auth = _CountingAuth()
    for _ in range(10):
        with authenticated(auth, Credentials()):
            pass
    assert auth.calls == 10


def test_argon2_guard_rejects_zero_max_concurrent() -> None:
    """max_concurrent=0 是非法，装配期炸，不用 or 吞成默认（审计验收 P2-guard）。"""
    from security.concurrency_guard import Argon2Guard, reset_guard

    reset_guard()
    with pytest.raises(ValueError):
        Argon2Guard(max_concurrent=0)
    reset_guard()


def test_argon2_guard_conflicting_config_raises() -> None:
    """同进程重复装配不同 max_concurrent 报错，不静默忽略（审计验收 P2-guard）。"""
    from security.concurrency_guard import default_argon2_guard, reset_guard

    reset_guard()
    default_argon2_guard(max_concurrent=2)
    with pytest.raises(ValueError):
        default_argon2_guard(max_concurrent=4)
    # 相同配置不报错
    default_argon2_guard(max_concurrent=2)
    reset_guard()


def test_argon2_guard_concurrency_is_actually_bounded() -> None:
    """真实并发测试：同时进入 authenticate 的数 <= max_concurrent。

    复验 P3：此前 gate.set() 没等前两个确定占住槽，调度型竞态导致偶发
    ``assert 1 == 2``。改为 Barrier 明确同步 happens-before：
    1) 先启 2 线程，等它们都进 _Blocking.authenticate（占住两个槽）；
    2) 再启 2 线程，它们应被 guard 挡（acquire 失败 -> 429）；
    3) 最后 set gate 释放前两个。
    """
    import threading

    from security.concurrency_guard import Argon2Guard

    guard = Argon2Guard(max_concurrent=2)
    in_flight = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()
    # 前 2 个线程进入 authenticate 后用它通知主线程「已占住槽」
    holders_inside = threading.Barrier(2)
    holders_ready = threading.Event()

    class _Blocking:
        mode = DevAuthenticator().mode

        @staticmethod
        def authenticate(credentials):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            # 通知主线程：我已占住槽。用 Barrier 让 2 个 holder 都到齐再统一放行。
            try:
                holders_inside.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            holders_ready.set()
            gate.wait(timeout=3)
            with lock:
                in_flight -= 1
            return DevAuthenticator().authenticate(credentials)

    def fire(i, results):
        try:
            with authenticated(_Blocking(), Credentials(), None, None, argon2_guard=guard):
                results.append(i)
        except RateLimitedError:
            results.append(f"blocked-{i}")

    # 阶段 1：先启 2 个占槽线程，等它们都进入 authenticate
    holder_results = []
    holders = [threading.Thread(target=fire, args=(i, holder_results)) for i in range(2)]
    for t in holders:
        t.start()
    # 等两个 holder 都进 authenticate（Barrier 到齐 -> holders_ready set）
    assert holders_ready.wait(timeout=3), "holders 未在限时内占住槽"
    # 此时两个槽被占

    # 阶段 2：再启 2 个线程，应被 guard 挡（429）
    blocked_results = []
    seekers = [threading.Thread(target=fire, args=(i, blocked_results)) for i in (2, 3)]
    for t in seekers:
        t.start()
    for t in seekers:
        t.join(timeout=2)

    # 阶段 3：放行前两个
    gate.set()
    for t in holders:
        t.join(timeout=2)

    blocked = [x for x in blocked_results if isinstance(x, str)]
    accepted = [x for x in blocked_results if isinstance(x, int)]
    assert accepted == [], "槽位已满时 seeker 不应进入 authenticate"
    assert len(blocked) == 2, f"应有 2 个被挡，得到 {blocked_results}"
    assert peak == 2
