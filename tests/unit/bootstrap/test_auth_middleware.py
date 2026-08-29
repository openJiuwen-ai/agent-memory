"""bootstrap/core/auth_middleware：凭据归一 + 请求作用域生命周期。

接口先行版：用 stub Authenticator / limiter / guard 验证中间件的**编排顺序**
（限流 -> 并发预算 -> 认证 -> 构造请求上下文）与凭据归一逻辑；
认证算法本身属 ``authentication_impl``，未合入。
"""

# ruff: noqa: E402

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

from jiuwen_memory.common.errors import AuthenticationError  # noqa: E402
from jiuwen_memory.common.security.types import (  # noqa: E402
    AuthContext,
    Credentials,
    RequestSecurityContext,
    Surface,
)
from jiuwen_memory.common.type_def.scope import Scope  # noqa: E402

pytestmark = pytest.mark.unit


class _StubAuthenticator:
    """只记录调用顺序与凭据，不实现任何认证算法。"""

    def __init__(self, calls: list | None = None) -> None:
        self.calls = calls if calls is not None else []

    def authenticate(self, credentials: Credentials) -> AuthContext:
        self.calls.append(("authenticate", credentials.api_key))
        return AuthContext(actor=Scope(org="acme", user="alice"), auth_method="stub")

    @staticmethod
    def mode() -> str:
        return "stub"


class _OrderRecorder:
    """limiter / guard 共用：记录被调用的相对顺序与放行决定。"""

    def __init__(self, name: str, *, allow: bool = True, calls: list | None = None) -> None:
        self._name = name
        self._allow = allow
        self._calls = calls if calls is not None else []

    def hit(self) -> bool:
        self._calls.append(self._name)
        return self._allow


class _StubLimiter:
    def __init__(self, *, allow: bool = True, calls: list | None = None) -> None:
        self._recorder = _OrderRecorder("limiter", allow=allow, calls=calls)

    def allow(self, peer: str) -> bool:
        return self._recorder.hit()


class _StubGuard:
    def __init__(self, *, allow: bool = True, calls: list | None = None) -> None:
        self._recorder = _OrderRecorder("guard", allow=allow, calls=calls)
        self.released = False

    def acquire(self) -> bool:
        return self._recorder.hit()

    def release(self) -> None:
        self.released = True


# -- credentials_from_headers ------------------------------------------------- #


def test_bearer_token_is_extracted() -> None:
    creds = credentials_from_headers({"Authorization": "Bearer sk-123"})
    assert creds.api_key == "sk-123"


def test_x_api_key_fallback() -> None:
    creds = credentials_from_headers({"X-Api-Key": " sk-456 "})
    assert creds.api_key == "sk-456"


def test_header_keys_are_lowercased() -> None:
    """header 名归一小写：authenticator 侧按小写常量查，两边不必各写一次 lower。"""
    creds = credentials_from_headers({"AUTHORIZATION": "Bearer sk-1"})
    assert creds.api_key == "sk-1"
    assert creds.headers == {"authorization": "Bearer sk-1"}


def test_missing_credentials_yield_empty_api_key() -> None:
    creds = credentials_from_headers({"Content-Type": "application/json"}, peer_address="1.2.3.4")
    assert creds.api_key == ""
    assert creds.peer_address == "1.2.3.4"


def test_non_bearer_authorization_is_ignored() -> None:
    creds = credentials_from_headers({"Authorization": "Basic dXNlcjpwYXNz"})
    assert creds.api_key == ""


# -- authenticated() 编排顺序 -------------------------------------------------- #


def test_authentication_order_limiter_guard_then_authenticate() -> None:
    """限流在认证之前：认证本身是要保护的资源（F05 §请求执行流程）。"""
    calls: list[str] = []
    authenticator = _StubAuthenticator(calls=calls)
    limiter = _StubLimiter(calls=calls)
    guard = _StubGuard(calls=calls)

    with authenticated(
        authenticator,
        Credentials(api_key="k"),
        limiter=limiter,
        workload_guard=guard,
    ):
        calls.append("yield")

    assert calls == ["limiter", "guard", ("authenticate", "k"), "yield"]


def test_rate_limited_rejects_before_authenticating() -> None:
    from jiuwen_memory.common.errors import RateLimitedError

    calls: list[str] = []
    authenticator = _StubAuthenticator(calls=calls)

    with pytest.raises(RateLimitedError):
        with authenticated(
            authenticator,
            Credentials(api_key="k"),
            limiter=_StubLimiter(allow=False, calls=calls),
        ):
            pass  # pragma: no cover - 不会执行到

    assert calls == ["limiter"]  # 认证从未发生


def test_guard_exhaustion_rejects_before_authenticating() -> None:
    from jiuwen_memory.common.errors import RateLimitedError

    calls: list[str] = []
    authenticator = _StubAuthenticator(calls=calls)

    with pytest.raises(RateLimitedError):
        with authenticated(
            authenticator,
            Credentials(api_key="k"),
            workload_guard=_StubGuard(allow=False, calls=calls),
        ):
            pass  # pragma: no cover - 不会执行到

    assert calls == ["guard"]


def test_guard_released_when_authentication_fails() -> None:
    """acquire 成功后必须在 finally 中 release：认证失败也不能泄漏槽位。"""

    class _Failing(_StubAuthenticator):
        def authenticate(self, credentials: Credentials) -> AuthContext:
            self.calls.append("authenticate")
            raise AuthenticationError("authentication failed")

    guard = _StubGuard()
    with pytest.raises(AuthenticationError):
        with authenticated(_Failing(), Credentials(api_key="k"), workload_guard=guard):
            pass  # pragma: no cover

    assert guard.released is True


def test_context_var_is_reset_after_scope_exits() -> None:
    """退出请求作用域后 get_current() 必须回到 None：线程池复用下漏 reset 即越权。

    ContextVar 里是 AuthContext（日志/trace 用），yield 出来的是完整的
    RequestSecurityContext（授权用）--两者不是同一个对象。
    """
    from jiuwen_memory.common.security.types import get_current

    authenticator = _StubAuthenticator()
    with authenticated(authenticator, Credentials(api_key="k")) as security:
        assert get_current() is security.auth
    assert get_current() is None


# -- RequestSecurityContext 构造（迁移计划 §5.2 第 7 项）----------------------- #


def test_authenticated_yields_request_security_context() -> None:
    """中间件的产出是 RequestSecurityContext：MemoryAPI 公开方法的唯一安全输入。"""
    authenticator = _StubAuthenticator()
    with authenticated(authenticator, Credentials(api_key="k")) as security:
        assert isinstance(security, RequestSecurityContext)
        assert security.actor == Scope(org="acme", user="alice")
        assert security.auth.auth_method == "stub"


def test_context_comes_from_the_controlled_constructor() -> None:
    """中间件产出的上下文必须通过受控构造入口绑定来源。"""
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
    from jiuwen_memory.common.security.types import get_current

    authenticator = _StubAuthenticator()
    with pytest.raises(RuntimeError):
        with authenticated(authenticator, Credentials(api_key="k")):
            raise RuntimeError("boom")
    assert get_current() is None
