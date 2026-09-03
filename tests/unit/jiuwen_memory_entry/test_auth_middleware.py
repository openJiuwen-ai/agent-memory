"""jiuwen_memory_entry/core/auth_middleware：凭据归一 + ContextVar 生命周期。

中间件本身不含认证策略（模式由配置在装配期选定），故这里测的只有两件事：
**凭据材料被正确归一**，以及**上下文一定被清理**。后者是 05 最容易出的 bug，
测法是验证行为后果（``get_current()`` 是否干净），不是验证 ``reset_current``
被调用过。
"""

# ruff: noqa: E402

from __future__ import annotations

import os
import sys

import pytest

# jiuwen_memory_entry/core 是 flat import root（不是包），与各 surface 用同样的方式接进来。
_CORE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "jiuwen_memory_entry", "core")
)
if _CORE_DIR not in sys.path:
    sys.path.append(_CORE_DIR)

from auth_middleware import authenticated, credentials_from_headers  # noqa: E402

from jiuwen_memory.common.bootstrap import register_plugins  # noqa: E402
from jiuwen_memory.common.errors import AuthenticationError, RateLimitedError  # noqa: E402
from jiuwen_memory.common.security.authentication.authentication_impl.api_key_authenticator import (
    ApiKeyAuthenticator,  # noqa: E402
)
from jiuwen_memory.common.security.authentication.authentication_impl.dev_authenticator import (
    DevAuthenticator,  # noqa: E402
)
from jiuwen_memory.common.security.authentication.authentication_impl.trusted_authenticator import (
    TrustedAuthenticator,  # noqa: E402
)
from jiuwen_memory.common.security.authentication.key_store import KeyStoreProducer  # noqa: E402
from jiuwen_memory.common.security.protection.protection_impl.semaphore_guard import (
    SemaphoreWorkloadGuard,  # noqa: E402
)
from jiuwen_memory.common.security.types import (  # noqa: E402
    AuthContext,
    Credentials,
    RequestSecurityContext,
    Role,
    Surface,
    get_current,
)
from jiuwen_memory.common.type_def.scope import Scope  # noqa: E402
from jiuwen_memory.config.context import AssemblyContext  # noqa: E402

pytestmark = pytest.mark.unit

_ALICE = Scope(org="acme", user="alice")


@pytest.fixture(scope="module")
def key_store():
    register_plugins()
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
        # ContextVar 挂 AuthContext（日志/trace 用），yield 的是 RequestSecurityContext
        assert get_current() is ctx.auth
        assert ctx.auth.role is Role.ROOT
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
        assert ctx.auth.role is Role.USER


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
        assert ctx.auth.role is Role.ROOT
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
    assert set(detail) == {"mode", "peer"}
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


# -- 昂贵操作的并发预算（审计 P1-3；F05 §Protection §WorkloadGuard）---------- #


def test_workload_guard_release_on_success() -> None:
    """guard 在认证成功后必须释放，否则槽位泄漏把后续请求也堵死。"""
    guard = SemaphoreWorkloadGuard(1)
    auth = _CountingAuth()
    with authenticated(auth, Credentials(), None, None, workload_guard=guard):
        pass
    # 认证后槽位已释放，能再 acquire
    assert guard.acquire() is True
    guard.release()
    assert auth.calls == 1


def test_workload_guard_release_on_auth_failure() -> None:
    """认证失败也要释放（finally）。"""
    guard = SemaphoreWorkloadGuard(1)

    # 用一个恒失败的 auth：走认证失败路径，验证 guard 在 finally 释放
    class _Fail:
        mode = DevAuthenticator().mode

        @staticmethod
        def authenticate(credentials):
            raise AuthenticationError("nope")

    with pytest.raises(AuthenticationError):
        with authenticated(_Fail(), Credentials(), None, None, workload_guard=guard):
            pass  # pragma: no cover
    assert guard.acquire() is True
    guard.release()


def test_workload_guard_blocks_when_slots_exhausted() -> None:
    """耗尽并发槽返回 429，不进入 authenticate。"""
    guard = SemaphoreWorkloadGuard(1)
    # 占满唯一槽位
    assert guard.acquire() is True
    auth = _CountingAuth()
    with pytest.raises(RateLimitedError):
        with authenticated(auth, Credentials(), None, None, workload_guard=guard):
            pass  # pragma: no cover
    assert auth.calls == 0
    guard.release()


def test_workload_guard_released_on_rate_limit_before_it() -> None:
    """IP 桶先挡住时 guard 不该 acquire（两层独立）。"""
    guard = SemaphoreWorkloadGuard(1)
    with pytest.raises(RateLimitedError):
        with authenticated(DevAuthenticator(), _peer(), None, _Blocked(), workload_guard=guard):
            pass  # pragma: no cover
    # guard 没被占
    assert guard.acquire() is True
    guard.release()


def test_workload_guard_none_means_unlimited() -> None:
    """None 表示不限（DEV / 进程内直连），与一期行为一致。"""
    auth = _CountingAuth()
    for _ in range(10):
        with authenticated(auth, Credentials()):
            pass
    assert auth.calls == 10


def test_workload_guard_rejects_zero_max_concurrent() -> None:
    """max_concurrent=0 是非法，装配期炸，不用 or 吞成默认（审计验收 P2-guard）。"""
    with pytest.raises(ValueError):
        SemaphoreWorkloadGuard(0)


def test_workload_guard_concurrency_is_actually_bounded() -> None:
    """真实并发测试：同时进入 authenticate 的数 <= max_concurrent。

    复验 P3：此前 gate.set() 没等前两个确定占住槽，调度型竞态导致偶发
    ``assert 1 == 2``。改为 Barrier 明确同步 happens-before：
    1) 先启 2 线程，等它们都进 _Blocking.authenticate（占住两个槽）；
    2) 再启 2 线程，它们应被 guard 挡（acquire 失败 -> 429）；
    3) 最后 set gate 释放前两个。
    """
    import threading

    guard = SemaphoreWorkloadGuard(2)
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
            with authenticated(_Blocking(), Credentials(), None, None, workload_guard=guard):
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


# -- RequestSecurityContext 构造（迁移计划 §5.2 第 7 项）----------------------- #


class _StubAuthenticator:
    """只产出固定 AuthContext，不实现任何认证算法（契约测试用）。"""

    @staticmethod
    def authenticate(credentials: Credentials) -> AuthContext:
        return AuthContext(actor=_ALICE, auth_method="stub")

    @staticmethod
    def mode() -> str:
        return "stub"


def test_authenticated_yields_request_security_context() -> None:
    """中间件的产出是 RequestSecurityContext：MemoryAPI 公开方法的唯一安全输入。"""
    authenticator = _StubAuthenticator()
    with authenticated(authenticator, Credentials(api_key="k")) as security:
        assert isinstance(security, RequestSecurityContext)
        assert security.actor == Scope(org="acme", user="alice")
        assert security.auth.auth_method == "stub"


def test_context_comes_from_the_controlled_constructor() -> None:
    """中间件产出的上下文必须通过受控构造入口绑定来源。

    直接 ``RequestSecurityContext(...)`` 拿不到有效 ``_origin``——PEP 会把这样的
    上下文判为未受控。这条断言守的就是「中间件走的是 ``new_request_context``」。
    """
    with authenticated(_StubAuthenticator(), Credentials(api_key="k")) as security:
        assert security.has_valid_origin()


def test_request_id_is_server_generated_and_unique() -> None:
    """request_id 必须由服务端生成，且在请求之间保持唯一。"""
    seen = set()
    for _ in range(3):
        with authenticated(_StubAuthenticator(), Credentials(api_key="k")) as security:
            assert security.request_id
            seen.add(security.request_id)
    assert len(seen) == 3


def test_surface_comes_from_the_adapter_not_the_caller() -> None:
    """surface 由适配层写入；缺省 INTERNAL 对应进程内装配。"""
    with authenticated(
        _StubAuthenticator(), Credentials(api_key="k"), surface=Surface.HTTP
    ) as security:
        assert security.surface is Surface.HTTP
    with authenticated(_StubAuthenticator(), Credentials(api_key="k")) as security:
        assert security.surface is Surface.INTERNAL


def test_peer_is_the_transport_address_not_a_forwarded_header() -> None:
    """peer 仅采信传输层对端地址，不采信调用方可控的转发头。"""
    creds = credentials_from_headers(
        {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"}, "10.0.0.7"
    )
    with authenticated(_StubAuthenticator(), creds) as security:
        assert security.peer == "10.0.0.7"


def test_attributes_are_empty_and_read_only() -> None:
    """本层没有可信系统属性，attributes 必须为空且只读。"""
    with authenticated(_StubAuthenticator(), Credentials(api_key="k")) as security:
        assert dict(security.attributes) == {}
        with pytest.raises(TypeError):
            security.attributes["injected"] = "root"  # type: ignore[index]


def test_started_at_is_server_clock() -> None:
    """授权的时效判定用它派生的 now，必须是服务端时钟且带时区。"""
    with authenticated(_StubAuthenticator(), Credentials(api_key="k")) as security:
        assert security.started_at is not None
        assert security.started_at.tzinfo is not None


def test_context_var_is_reset_even_when_body_raises() -> None:
    authenticator = _StubAuthenticator()
    with pytest.raises(RuntimeError):
        with authenticated(authenticator, Credentials(api_key="k")):
            raise RuntimeError("boom")
    assert get_current() is None


# -- actor 形态在认证边界统一校验（IMPL-01 §1.1） ------------------------------ #


class _MalformedActorAuth:
    """第三方认证器桩：产出形态非法的 actor，绕过内置认证器的形态保证。"""

    def __init__(self, actor) -> None:
        self._actor = actor

    def authenticate(self, credentials: Credentials) -> AuthContext:
        return AuthContext(actor=self._actor, auth_method="malformed")

    @staticmethod
    def mode() -> str:
        return "malformed"


def test_malformed_actor_form_fails_closed_at_the_boundary() -> None:
    """认证器返回了非法 actor 时，中间件必须拒绝，不能把上下文挂上去。

    这是全局不变量的执行点：第三方 Authenticator 绕过内置形态保证，也要在
    这里 fail-closed。双主体 / 无主体 / 孤立 session 三类都必须拒。
    """
    for bad_actor in (
        Scope(org="acme", user="alice", agent="bot"),  # 双主体
        Scope(org="acme"),  # 无主体
        Scope(org="acme", session="s1"),  # 孤立 session
        Scope(),  # 空 Scope
    ):
        authenticator = _MalformedActorAuth(bad_actor)
        with pytest.raises(AuthenticationError):
            with authenticated(authenticator, Credentials(api_key="k")):
                pass  # pragma: no cover - 进入 with 体之前就抛了
        assert get_current() is None


def test_malformed_actor_form_denial_is_audited() -> None:
    """形态拒绝与认证失败同样落一条入口拒绝审计（security.md §7.2）。"""

    class _Recorder:
        def __init__(self) -> None:
            self.events = []

        def record(self, event) -> None:
            self.events.append(event)

    recorder = _Recorder()
    with pytest.raises(AuthenticationError):
        with authenticated(
            _MalformedActorAuth(Scope(org="acme")), Credentials(api_key="k"), recorder
        ):
            pass  # pragma: no cover
    assert len(recorder.events) == 1
    assert recorder.events[0].action == "authenticate"
    assert recorder.events[0].decision == "deny"


def test_named_dev_actor_passes_the_boundary() -> None:
    """具名 DEV 主体（org=system, user=dev）本身是合法形态，正常通过校验。"""
    with authenticated(DevAuthenticator(), Credentials()) as security:
        assert security.actor == Scope(org="system", user="dev")


def test_successful_authentication_is_audited(key_store, alice_key) -> None:
    """AUTH-ENC-06：认证成功也落一条 allow 审计，带认证元数据与请求定位字段。"""
    recorded = []

    class _Recorder:
        @staticmethod
        def record(event):
            recorded.append(event)

    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="", name="test")
    with authenticated(
        auth,
        Credentials(api_key=alice_key, peer_address="10.0.0.9"),
        _Recorder(),
        surface=Surface.HTTP,
    ) as ctx:
        success = [e for e in recorded if e.action == "authenticate"]
        assert len(success) == 1
        event = success[0]
        assert event.decision == "allow"
        assert event.layer == "security"
        assert event.actor == ctx.auth.actor
        assert event.detail["peer"] == "10.0.0.9"
        assert event.detail["surface"] == "http"
        assert event.detail["request_id"] == ctx.request_id
        assert event.detail["role"] == "user"
        assert event.detail["key_fp"]  # 指纹非空：可关联，但非明文
        assert event.detail["auth_mode"] == "api_key"
        # 审计记的是实际执行者：acting_user 取 actor.user（AuthContext 已无同名字段）。
        assert event.detail["acting_user"] == "alice"
    # 认证成功事件先于业务事件产生，request_id 可用于事后关联。
    assert recorded[0].action == "authenticate"


def test_no_audit_recorder_means_no_success_event(key_store, alice_key) -> None:
    """audit=None（进程内直连）时不落认证成功事件，行为与拒绝路径一致。"""
    auth = ApiKeyAuthenticator(key_store=key_store, root_api_key="", name="test")
    with authenticated(auth, Credentials(api_key=alice_key)) as ctx:
        assert ctx.auth.actor.user == "alice"
