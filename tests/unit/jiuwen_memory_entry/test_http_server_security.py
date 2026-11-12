# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP transport authentication and API contract boundary tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    AuthenticationError,
    BackendError,
    ConflictError,
    HealthCheckError,
    NotFoundError,
    PartialFailureError,
    PermissionDeniedError,
    RateLimitedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authorization.authorization_impl.allow_all_authorizer import (
    AllowAllAuthorizer,
)
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    Role,
    get_current,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (
    AllowAllPermissionManager,
)
from jiuwen_memory.control.types import PermissionContext
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime
from jiuwen_memory_entry.portal.requests import invoke_browser_api
from tests.support.scoped_authenticator import ScopedAuthenticator
from tests.unit.jiuwen_memory_entry.fixtures import (
    DEV_MANAGEMENT_CASES,
    fixed_dev_management_config,
    seed_dev_management_space,
    upstream_api_error,
)

pytestmark = pytest.mark.unit
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Authenticator:
    def __init__(
        self,
        actor: Scope | None = None,
        *,
        role: Role = Role.USER,
        fail: bool = False,
    ) -> None:
        self.actor = actor or Scope(org="acme", user="alice")
        self.role = role
        self.fail = fail

    def authenticate(self, credentials):
        if self.fail or not credentials.api_key:
            raise AuthenticationError("authentication failed")
        return AuthContext(actor=self.actor, role=self.role, auth_method="test")

    @staticmethod
    def mode() -> str:
        return "test"


class _RejectingLimiter:
    @staticmethod
    def allow(_peer: str) -> bool:
        return False


class _LoopbackAuthenticator(Authenticator):
    @staticmethod
    def authenticate(credentials) -> AuthContext:
        del credentials
        return AuthContext(actor=Scope(org="acme", user="alice"))

    @staticmethod
    def mode() -> str:
        return "third_party"

    @staticmethod
    def health() -> None:
        return None


class _RemoteAuthenticator(_LoopbackAuthenticator):
    @staticmethod
    def requires_loopback_binding() -> bool:
        return False


class _RejectingBindingPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def check(self, host: str, *, requires_loopback: bool) -> None:
        self.calls.append((host, requires_loopback))
        raise ValidationError("binding policy rejected host")


class _RecordingAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class _RecordingPermissionManager(AllowAllPermissionManager):
    def __init__(self) -> None:
        super().__init__()
        self.checks: list[tuple[Scope, Scope, Action]] = []

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        del context
        self.checks.append((actor, target, action))
        return True


_RECORDING_PERMISSION_MANAGERS: list[_RecordingPermissionManager] = []


class _DenyingPermissionManager(AllowAllPermissionManager):
    def __init__(self) -> None:
        super().__init__()
        self.checks: list[tuple[Scope, Scope, Action]] = []

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        del context
        self.checks.append((actor, target, action))
        return action is not Action.WRITE


_DENYING_PERMISSION_MANAGERS: list[_DenyingPermissionManager] = []


@PermissionProducer.register("http_server_security_recording")
def _build_recording_permission_manager(_config) -> _RecordingPermissionManager:
    manager = _RecordingPermissionManager()
    _RECORDING_PERMISSION_MANAGERS.append(manager)
    return manager


@PermissionProducer.register("http_server_security_denying")
def _build_denying_permission_manager(_config) -> _DenyingPermissionManager:
    manager = _DenyingPermissionManager()
    _DENYING_PERMISSION_MANAGERS.append(manager)
    return manager


@pytest.fixture(name="binding_httpd")
def binding_httpd_fixture(monkeypatch):
    instances = []

    class _NonBlockingHttpd:
        def __init__(self, address, handler) -> None:
            self.address = address
            self.handler = handler
            self.served = False
            self.closed = False
            instances.append(self)

        def serve_forever(self) -> None:
            self.served = True

        def server_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(http_server_module, "ThreadingHTTPServer", _NonBlockingHttpd)
    return instances


@pytest.fixture
def http_endpoint():
    runtime = SimpleNamespace(
        authenticator=_Authenticator(),
        rate_limiter=None,
        workload_guard=None,
        audit=None,
    )
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


@pytest.fixture
def http_endpoint_without_security_runtime():
    server = HttpServer.build(load_config([OFFLINE]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


@pytest.fixture
def dev_http_endpoint():
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=build_dev_security_runtime())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


def test_explicit_security_runtime_authorizer_is_the_pep_authorizer() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    """显式 Runtime 也只能有一个 PDP，不能因装配纪元不同而分叉 Store 视图。"""
    runtime = build_dev_security_runtime()
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)
    try:
        assert server.api._authorizer is runtime.authorizer
    finally:
        server.close(wait=True)


def test_explicit_test_only_authorizer_is_rejected() -> None:
    runtime = SimpleNamespace(
        authenticator=_Authenticator(),
        authorizer=AllowAllAuthorizer(),
    )
    with pytest.raises(ValidationError, match="test-only authorizer"):
        HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)


@pytest.fixture
def http_endpoint_with_recording_permission():
    _RECORDING_PERMISSION_MANAGERS.clear()
    runtime = SimpleNamespace(
        authenticator=_Authenticator(role=Role.ROOT),
        rate_limiter=None,
        workload_guard=None,
        audit=None,
    )
    config = load_config(
        [
            OFFLINE,
            {"memory_api": {"permission": {"default": "http_server_security_recording"}}},
        ]
    )
    server = HttpServer.build(config, security_runtime=runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


@pytest.fixture
def http_endpoint_with_denying_permission():
    _DENYING_PERMISSION_MANAGERS.clear()
    runtime = SimpleNamespace(
        authenticator=_Authenticator(actor=Scope(org="acme", user="root")),
        rate_limiter=None,
        workload_guard=None,
        audit=None,
    )
    config = load_config(
        [
            OFFLINE,
            {"memory_api": {"permission": {"default": "http_server_security_denying"}}},
        ]
    )
    server = HttpServer.build(config, security_runtime=runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


def _post(base_url: str, body: object, *, key: str = "test-key") -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/add",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _request(
    base_url: str,
    path: str,
    data: bytes | None,
    *,
    key: str | None = "test-key",
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, str]:
    status, body, response_headers = _request_with_headers(
        base_url, path, data, key=key, headers=headers
    )
    return status, body, response_headers.get("content-type", "").split(";", 1)[0]


def _request_with_headers(
    base_url: str,
    path: str,
    data: bytes | None,
    *,
    key: str | None = "test-key",
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> tuple[int, Any, dict[str, str]]:
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=request_headers, method=method
    )
    try:
        with _NO_PROXY_OPENER.open(request, timeout=3) as response:
            return (
                response.status,
                json.loads(response.read()),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            json.loads(exc.read()),
            {key.lower(): value for key, value in exc.headers.items()},
        )


def test_http_uses_authenticated_actor_and_api_scope(http_endpoint) -> None:
    base_url, server = http_endpoint
    status, body = _post(
        base_url,
        {"scope": {"org": "acme", "user": "alice"}, "content": "hello"},
    )

    assert status == 200, body
    assert isinstance(body, list)
    assert body[0]["scope"]["user"] == "alice"
    assert "request_id" not in body[0]
    actor = Scope(org="acme", user="alice")
    items = server.api.list(
        Scope(org="acme", user="alice"), security=internal_context(ScopedAuthenticator(actor))
    ).items
    assert len(items) == 1
    assert items[0].scope == Scope(org="acme", user="alice")


def test_http_rejects_actor_claim_before_api_call(http_endpoint) -> None:
    base_url, server = http_endpoint
    status, body = _post(
        base_url,
        {
            "scope": {"org": "acme", "user": "alice"},
            "content": "must not write",
            "actor_scope": "root",
        },
    )

    assert status == 400
    assert "authentication" in body["message"]
    assert body["request_id"]
    assert (
        server.api.list(
            Scope(org="acme", user="alice"),
            security=internal_context(ScopedAuthenticator(Scope(org="acme", user="alice"))),
        ).items
        == []
    )


def test_http_rejects_nested_identity_claim_before_api_call(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def add(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, body = _post(
        base_url,
        {
            "scope": {"org": "acme", "user": "alice", "identity": "root"},
            "content": "must not write",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert body["request_id"]
    assert called is False


def test_http_missing_credentials_returns_401(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def add(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, response, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
        key=None,
        headers={"Content-Type": "application/json"},
    )

    assert status == 401
    assert response["error"] == "AuthenticationError"
    assert response["message"] == "authentication failed"
    assert response["request_id"]
    assert response["retryable"] is False
    assert headers["x-request-id"] == response["request_id"]
    assert called is False


def test_http_authentication_denial_audit_shares_edge_request_id(http_endpoint) -> None:
    base_url, server = http_endpoint
    audit = _RecordingAudit()
    server.security_runtime.audit = audit

    status, body, _ = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
        key=None,
    )

    assert status == 401
    assert audit.events[-1].action == "authenticate"
    assert audit.events[-1].detail["request_id"] == body["request_id"]


def test_http_invalid_credentials_returns_401_before_dto(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.authenticator.fail = True
    called = False

    def add(*args, **kwargs):
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, body, _ = _request(base_url, "/v1/add", b"not-json", key="bad")

    assert status == 401
    assert body["error"] == "AuthenticationError"
    assert body["request_id"]
    assert called is False


def test_http_unknown_field_is_400_and_does_not_call_api(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def add(*args, **kwargs):
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, body = _post(
        base_url,
        {"scope": {"org": "acme"}, "content": "x", "unexpected": True},
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert called is False


@pytest.mark.parametrize("body", [None, [], "text", 42])
def test_http_rejects_non_object_json_body(http_endpoint, body) -> None:
    base_url, _ = http_endpoint
    status, response = _post(base_url, body)

    assert status == 400
    assert response["error"] == "ValidationError"
    assert response["request_id"]


def test_http_without_security_runtime_fails_closed(
    http_endpoint_without_security_runtime,
) -> None:
    base_url, server = http_endpoint_without_security_runtime
    called = False

    def add(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, response = _post(
        base_url,
        {"scope": {"org": "acme"}, "content": "must not execute"},
    )

    assert status == 503
    assert response["error"] == "SecurityUnavailable"
    assert response["request_id"]
    assert response["retryable"] is False
    assert called is False


def test_dev_authentication_accepts_request_without_credentials(dev_http_endpoint) -> None:
    base_url, _ = dev_http_endpoint
    status, body, _ = _request(
        base_url,
        "/v1/add",
        json.dumps(
            {
                "content": "development request",
                "scope": {"org": "local", "user": "developer"},
            }
        ).encode(),
        key=None,
    )

    assert status == 200, body
    assert body[0]["scope"]["org"] == "local"
    assert body[0]["scope"]["user"] == "developer"


@pytest.mark.parametrize("role", [None, "root"], ids=["default-user", "explicit-root"])
@pytest.mark.parametrize(
    "scope",
    [
        {"org": "other-org-acc52", "user": "developer"},
        {"org": "local", "user": "other-user-acc52"},
    ],
    ids=["cross-org", "cross-user"],
)
def test_fixed_dev_scope_boundary_requires_explicit_root(role, scope) -> None:
    """Issue #254：原无认证头请求默认 403，显式服务端 ROOT 才允许跨范围。"""
    layers = [OFFLINE]
    if role is not None:
        layers.append(
            {
                "memory_api": {
                    "security": {
                        "default": {
                            "target": "standard",
                            "params": {
                                "authenticator": {
                                    "target": "dev",
                                    "params": {"role": role},
                                }
                            },
                        }
                    }
                }
            }
        )
    server = HttpServer.build(with_local_dev_security(load_config(layers)))
    with ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls()) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{httpd.server_port}"
        try:
            for method, payload in (
                ("add", {"content": "issue254 boundary", "scope": scope}),
                ("search", {"query": "issue254", "context": {"scope": scope}, "top_k": 10}),
            ):
                status, body, _ = _request(
                    base_url, f"/v1/{method}", json.dumps(payload).encode(), key=None
                )
                assert status == (200 if role == "root" else 403), body
            assert server.security_runtime.authenticator.requires_loopback_binding() is True
            with pytest.raises(ValidationError, match="loopback"):
                server.security_runtime.binding_policy.check("0.0.0.0", requires_loopback=True)
        finally:
            httpd.shutdown()
            thread.join(timeout=2)
            server.close(wait=True)


def test_default_dev_cannot_elevate_via_request_material(dev_http_endpoint) -> None:
    base_url, _ = dev_http_endpoint
    status, body, _ = _request(
        base_url,
        "/v1/add",
        json.dumps({"content": "no elevation", "scope": {"org": "other-org"}}).encode(),
        key="root",
        headers={"X-Role": "root", "X-Principal-Id": "root"},
    )
    assert status == 403, body


@pytest.mark.parametrize("role", [None, "admin", "root"], ids=["default-user", "admin", "root"])
def test_fixed_dev_management_role_boundaries(role) -> None:
    """同一真实 HTTP 入口覆盖审计、空间、成员、策略及系统管理的角色迁移。"""
    config = with_local_dev_security(load_config([OFFLINE, fixed_dev_management_config(role)]))
    server = HttpServer.build(config)
    seed_dev_management_space(server.api)
    with ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls()) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for method, payload, allowed_roles in DEV_MANAGEMENT_CASES:
                status, body, _ = _request(
                    f"http://127.0.0.1:{httpd.server_port}",
                    f"/v1/{method}",
                    json.dumps(payload).encode(),
                    key=None,
                )
                assert status == (200 if role in allowed_roles else 403), (method, body)
                if method == "verify_audit" and status == 200:
                    assert body["status"] == "unsupported", body
        finally:
            httpd.shutdown()
            thread.join(timeout=2)
            server.close(wait=True)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
@pytest.mark.parametrize(
    "identities", [None, {"test-u1": {"actor": {"org": "local", "user": "u1"}}}]
)
def test_dev_authentication_rejects_non_loopback_binding(host, identities, binding_httpd) -> None:
    server = HttpServer.build(
        load_config([OFFLINE]), security_runtime=build_dev_security_runtime(identities=identities)
    )
    with pytest.raises(ValidationError, match="loopback"):
        server.serve(host, 8137)

    assert binding_httpd == [], "unsafe binding must be rejected before creating a socket"


def test_dev_authentication_cli_rejects_non_loopback_binding(monkeypatch, binding_httpd) -> None:
    monkeypatch.delenv("JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK", raising=False)

    result = http_server_module.main(["--auth-mode", "dev", "--host", "0.0.0.0", "--port", "8137"])

    assert result == 2
    assert binding_httpd == []


@pytest.mark.parametrize("host,allowed", [("127.0.0.1", True), ("0.0.0.0", False)])
def test_dev_binding_is_checked_before_dreaming_restore(
    host, allowed, binding_httpd, monkeypatch
) -> None:
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=build_dev_security_runtime())
    restored = []
    monkeypatch.setattr(server, "restore_dreaming", lambda: restored.append(True))

    if allowed:
        server.serve(host, 8137)
    else:
        with pytest.raises(ValidationError, match="loopback"):
            server.serve(host, 8137)

    assert restored == ([True] if allowed else [])
    assert len(binding_httpd) == int(allowed)


def test_dev_authentication_cannot_bypass_binding_policy(binding_httpd) -> None:
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=build_dev_security_runtime())

    with pytest.raises(ValidationError, match="loopback"):
        server.serve("0.0.0.0", 8137)
    assert binding_httpd == []


def test_dev_binding_environment_variable_no_longer_bypasses_policy(
    monkeypatch, binding_httpd
) -> None:
    monkeypatch.setenv("JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK", "true")

    result = http_server_module.main(["--auth-mode", "dev", "--host", "0.0.0.0", "--port", "8137"])

    assert result == 2
    assert binding_httpd == []


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_third_party_loopback_requirement_uses_binding_policy(host, binding_httpd) -> None:
    runtime = replace(
        build_dev_security_runtime(),
        authenticator=_LoopbackAuthenticator(),
    )
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    with pytest.raises(ValidationError, match="loopback") as error:
        server.serve(host, 8137)

    assert "development authentication" not in str(error.value)
    assert "JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK" not in str(error.value)
    assert binding_httpd == []


def test_remote_capable_authenticator_binds_without_dev_override(binding_httpd) -> None:
    runtime = replace(
        build_dev_security_runtime(),
        authenticator=_RemoteAuthenticator(),
    )
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    server.serve("0.0.0.0", 8137)

    assert binding_httpd[0].served is True
    assert binding_httpd[0].closed is True


def test_binding_policy_denial_cannot_be_overridden(binding_httpd) -> None:
    policy = _RejectingBindingPolicy()
    runtime = SimpleNamespace(
        authenticator=build_dev_security_runtime().authenticator,
        binding_policy=policy,
    )
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    with pytest.raises(ValidationError, match="binding policy rejected host"):
        server.serve("0.0.0.0", 8137)

    assert policy.calls == [("0.0.0.0", True)]
    assert binding_httpd == []


def test_runtime_without_binding_policy_fails_closed(binding_httpd) -> None:
    runtime = SimpleNamespace(authenticator=_RemoteAuthenticator())
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    with pytest.raises(ValidationError, match="missing a binding policy"):
        server.serve("127.0.0.1", 8137)
    assert binding_httpd == []


@pytest.mark.parametrize(
    "http_settings", [None, [], {"dev_identities": None}, {"dev_identities": {}}]
)
def test_invalid_dev_identity_config_rejects_startup_before_binding(
    http_settings, monkeypatch, binding_httpd
) -> None:
    monkeypatch.setattr(http_server_module, "load_layer", lambda _path: {"http": http_settings})

    result = http_server_module.main(["--auth-mode", "dev", "test-config.json"])

    assert result == 2
    assert binding_httpd == []


def test_required_mode_does_not_activate_configured_dev_identities(monkeypatch) -> None:
    monkeypatch.setattr(
        http_server_module,
        "load_layer",
        lambda _path: {
            "http": {"dev_identities": {"test-ops": {"actor": {"org": "local"}}}},
        },
    )
    observed = []

    def capture_serve(server, host, port, *, allow_dev_non_loopback=False):
        del host, port, allow_dev_non_loopback
        observed.append(server.security_runtime)
        server.close()

    monkeypatch.setattr(HttpServer, "serve", capture_serve)

    assert http_server_module.main(["--auth-mode", "required", "test-config.json"]) == 0
    assert observed == [None]


def test_mapped_dev_keeps_real_permission_checks_and_request_identity() -> None:
    identities = {
        "test-u1": {"actor": {"org": "local", "user": "u1"}},
        "test-u2": {"actor": {"org": "local", "user": "u2"}},
    }
    original = load_config([OFFLINE])
    config = with_local_dev_security(original, identities=identities)
    assert "permission" not in config.settings["memory_api"]
    assert "security" not in (original.settings.get("memory_api") or {})
    server = HttpServer.build(config)
    with ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls()) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}"
        payload = {"content": "user one memory", "scope": {"org": "local", "user": "u1"}}
        try:
            assert _post(url, payload, key="test-u1")[0] == 200
            assert _post(url, payload, key="test-u2")[0] == 403
            assert _post(url, payload, key="unknown")[0] == 401
            assert _post(url, payload, key="")[0] == 401
            assert _post(url, payload, key="test-u1")[0] == 200
        finally:
            httpd.shutdown()
            thread.join(timeout=2)
            server.close(wait=True)


def test_dev_composition_preserves_explicit_security_and_permission() -> None:
    explicit_security = load_config(
        [
            OFFLINE,
            {
                "memory_api": {
                    "security": {"default": {"target": "standard"}},
                }
            },
        ]
    )
    assert with_local_dev_security(explicit_security) is explicit_security
    permission = {"default": "sqlite"}
    config = load_config([OFFLINE, {"memory_api": {"permission": permission}}])
    assert with_local_dev_security(config).settings["memory_api"]["permission"] == permission
    fixed = with_local_dev_security(load_config([OFFLINE]))
    assert "permission" not in fixed.settings["memory_api"]


def test_http_does_not_add_video_specific_add_parameters(http_endpoint) -> None:
    base_url, _ = http_endpoint
    status, body = _post(
        base_url,
        {
            "scope": {"org": "acme", "user": "alice"},
            "content": "video ingest",
            "source": "video",
            "uri": "file:///tmp/demo.mp4",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert "uri" in body["message"]


def test_http_delete_space_calls_same_named_api_with_original_parameters(
    http_endpoint, monkeypatch
) -> None:
    """The transport must pass decoded API parameters to the same-named method."""
    base_url, server = http_endpoint
    captured: dict[str, object] = {}

    def delete_space(org, space, *, security, mode):
        captured.update(org=org, space=space, security=security, mode=mode)
        return "deleted"

    monkeypatch.setattr(server.api, "delete_space", delete_space)
    status, body, _ = _request(
        base_url,
        "/v1/delete_space",
        json.dumps({"org": "acme", "space": "product", "mode": "purge"}).encode(),
    )

    assert status == 200, body
    assert body == "deleted"
    assert captured["org"] == "acme"
    assert captured["space"] == "product"
    assert captured["mode"].value == "purge"
    assert captured["security"].surface.value == "http"


def test_http_async_method_is_awaited_and_returns_raw_value(http_endpoint, monkeypatch) -> None:
    base_url, server = http_endpoint

    async def add_async(content, scope, *, security, **_kwargs):
        del content, security
        return [{"id": "async-unit", "scope": scope}]

    monkeypatch.setattr(server.api, "add_async", add_async)
    status, body, _ = _request(
        base_url,
        "/v1/add_async",
        json.dumps({"content": "hello", "scope": {"org": "acme", "user": "alice"}}).encode(),
    )

    assert status == 200
    assert body == [
        {
            "id": "async-unit",
            "scope": {"org": "acme", "space": "", "user": "alice", "agent": "", "session": ""},
        }
    ]


def test_http_none_return_value_is_serialized_as_json_null(http_endpoint, monkeypatch) -> None:
    base_url, server = http_endpoint

    def admin_set(key, value, *, security):
        del key, value, security
        return None

    monkeypatch.setattr(server.api, "admin_set", admin_set)
    status, body, _ = _request(
        base_url,
        "/v1/admin_set",
        json.dumps({"key": "feature", "value": "enabled"}).encode(),
    )

    assert status == 200
    assert body is None


def test_http_api_call_keeps_actor_and_target_separate_in_authorization_and_audit(
    http_endpoint_with_recording_permission,
) -> None:
    """Authenticated actor and API target remain independent through MemoryAPI."""
    base_url, server = http_endpoint_with_recording_permission
    actor = Scope(org="acme", user="root")
    target = Scope(org="acme", user="alice")
    server.security_runtime.authenticator.actor = actor

    status, body = _post(
        base_url,
        {"scope": {"org": "acme", "user": "alice"}, "content": "hello"},
    )

    assert status == 200, body
    assert _RECORDING_PERMISSION_MANAGERS[-1].checks == []
    events = server.api.audit(
        {"action": "add"}, security=internal_context(ScopedAuthenticator(actor, role=Role.ROOT))
    )
    event = next(event for event in events if event.action == "add")
    assert event.actor == actor
    assert event.target == target


def test_http_cross_scope_denial_uses_authenticated_actor(
    http_endpoint_with_denying_permission,
) -> None:
    base_url, server = http_endpoint_with_denying_permission
    actor = Scope(org="acme", user="root")
    target = Scope(org="acme", user="alice")

    status, body = _post(
        base_url,
        {
            "scope": {"org": "acme", "user": "alice"},
            "content": "must be denied",
            "actor_scope": "forged",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert _DENYING_PERMISSION_MANAGERS[-1].checks == []

    status, body = _post(
        base_url,
        {"scope": {"org": "acme", "user": "alice"}, "content": "must be denied"},
    )

    assert status == 403
    assert body["error"] == "PermissionDeniedError"
    assert body["message"] == "permission denied"
    # 非空间级记忆由 PR2 Authorizer 终局，旧 PermissionManager 不再参与判定。
    assert _DENYING_PERMISSION_MANAGERS[-1].checks == []
    events = server.api.audit(
        {"action": "add", "decision": "deny"},
        security=internal_context(ScopedAuthenticator(actor, role=Role.ROOT)),
    )
    event = next(event for event in events if event.target == target)
    assert event.actor == actor
    assert event.detail["request_id"] == body["request_id"]


@pytest.mark.parametrize("method", ["PUT", "BREW"])
def test_http_unsupported_method_uses_json_error_contract(http_endpoint, method: str) -> None:
    base_url, _ = http_endpoint
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b"{}",
        method=method,
    )

    assert status == 405
    assert body["error"] == "MethodNotAllowed"
    assert body["message"] == "method not allowed"
    assert body["retryable"] is False
    assert body["request_id"] == headers["x-request-id"]


def test_http_unexpected_error_log_is_redacted(http_endpoint, caplog) -> None:
    base_url, server = http_endpoint

    def failing_add(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(
            "Authorization: Bearer secret-token password=secret-password "
            "https://user:secret-password@example.com"
        )

    server.api.add = failing_add
    with caplog.at_level("ERROR", logger="agent-memory.server"):
        status, body, _ = _request_with_headers(
            base_url,
            "/v1/add",
            b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
        )

    assert status == 500
    assert body["message"] == "internal server error"
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-token" not in logs
    assert "secret-password" not in logs
    assert "Authorization" not in logs
    assert body["request_id"] in logs
    assert "RuntimeError" in logs


def test_http_requests_do_not_reuse_authenticated_actor(http_endpoint) -> None:
    base_url, server = http_endpoint
    expected = [Scope(org="acme", user="first"), Scope(org="acme", user="second")]
    actors = list(expected)
    observed: list[tuple[Scope, Scope]] = []

    class _RotatingAuthenticator(_Authenticator):
        def authenticate(self, credentials):
            self.actor = actors.pop(0)
            return super().authenticate(credentials)

    server.security_runtime.authenticator = _RotatingAuthenticator()

    def add(content, scope, *, security, **_kwargs):
        del content
        observed.append((scope, security.actor))
        return []

    server.api.add = add
    for scope in ("target-a", "target-b"):
        status, body = _post(
            base_url,
            {"scope": {"org": "acme", "user": scope}, "content": "x"},
        )
        assert status == 200, body

    assert observed == [
        (Scope(org="acme", user="target-a"), expected[0]),
        (Scope(org="acme", user="target-b"), expected[1]),
    ]


def test_http_clears_auth_context_on_all_request_outcomes(http_endpoint, monkeypatch) -> None:
    base_url, server = http_endpoint

    original_authenticated = http_server_module.authenticated
    contexts_after_exit: list[AuthContext | None] = []

    @contextmanager
    def recording_authenticated(*args, **kwargs):
        try:
            with original_authenticated(*args, **kwargs) as security:
                yield security
        finally:
            contexts_after_exit.append(get_current())

    monkeypatch.setattr(http_server_module, "authenticated", recording_authenticated)

    success_status, success_body = _post(
        base_url,
        {"scope": {"org": "acme", "user": "alice"}, "content": "x"},
    )
    assert success_status == 200, success_body

    dto_status, dto_body = _post(
        base_url,
        {
            "scope": {"org": "acme", "user": "alice"},
            "content": "x",
            "actor_scope": "forged",
        },
    )
    assert dto_status == 400, dto_body

    def failing_add(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("business failure")

    server.api.add = failing_add
    error_status, error_body = _post(
        base_url,
        {"scope": {"org": "acme", "user": "alice"}, "content": "x"},
    )
    assert error_status == 500, error_body

    server.security_runtime.authenticator.fail = True
    auth_status, auth_body, _ = _request(base_url, "/v1/add", b"not-json", key="bad")
    assert auth_status == 401, auth_body

    assert contexts_after_exit == [None, None, None, None]


def test_http_rate_limit_returns_429_before_dto_or_api_call(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.rate_limiter = _RejectingLimiter()
    called = False

    def add(*args, **kwargs):
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, body, content_type = _request(base_url, "/v1/add", b"not-json")

    assert status == 429
    assert body["request_id"]
    assert content_type == "application/json"
    assert called is False


def test_http_rate_limit_denial_audit_shares_edge_request_id(http_endpoint) -> None:
    base_url, server = http_endpoint
    audit = _RecordingAudit()
    server.security_runtime.audit = audit
    server.security_runtime.rate_limiter = _RejectingLimiter()

    status, body, _ = _request_with_headers(base_url, "/v1/add", b"not-json")

    assert status == 429
    assert audit.events[-1].action == "rate_limit"
    assert audit.events[-1].detail["request_id"] == body["request_id"]


def test_http_malformed_json_is_400_and_does_not_call_api(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def add(*args, **kwargs):
        nonlocal called
        called = True
        return []

    server.api.add = add
    status, body, _ = _request(base_url, "/v1/add", b"{invalid")

    assert status == 400
    assert body["error"] == "BadRequest"
    assert called is False


def test_http_oversized_body_returns_413_without_authentication(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.max_body_bytes = 2
    status, body, headers = _request_with_headers(base_url, "/v1/add", b"{}x")

    assert status == 413
    assert body["error"] == "PayloadTooLarge"
    assert body["retryable"] is False
    assert headers["x-request-id"] == body["request_id"]


def test_http_unknown_path_and_verb_return_404(http_endpoint) -> None:
    base_url, _ = http_endpoint
    status, body, content_type = _request(base_url, "/not-v1/add", b"{}")
    assert status == 404
    assert body["error"] == "NotFound"
    assert content_type == "application/json"

    status, body, _ = _request(base_url, "/v1/no_such_verb", b"{}")
    assert status == 404
    assert body["error"] == "UnknownVerb"


def test_healthz_is_json_and_does_not_require_authentication(http_endpoint) -> None:
    base_url, _ = http_endpoint
    request = urllib.request.Request(f"{base_url}/healthz", method="GET")
    with _NO_PROXY_OPENER.open(request, timeout=3) as response:
        body = json.loads(response.read())
        assert response.status == 200
        assert response.headers.get_content_type() == "application/json"
        assert response.headers["X-Request-ID"]
    assert body["status"] == "ok"
    assert "request_id" not in body


def test_http_error_request_ids_are_unique(http_endpoint) -> None:
    base_url, _ = http_endpoint
    ids = []
    for _ in range(2):
        status, body = _post(
            base_url,
            {"scope": {"org": "acme", "user": "alice"}, "content": "x", "extra": True},
        )
        assert status == 400
        ids.append(body["request_id"])
    assert len(set(ids)) == 2


def test_http_response_request_id_is_shared_by_body_and_header(http_endpoint) -> None:
    base_url, _ = http_endpoint
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b"{invalid",
    )

    assert status == 400
    assert headers["x-request-id"] == body["request_id"]
    assert headers["content-type"].startswith("application/json")


def test_http_ignores_client_request_id(http_endpoint) -> None:
    base_url, _ = http_endpoint
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b"{invalid",
        headers={"X-Request-ID": "client-supplied-id"},
    )

    assert status == 400
    assert body["request_id"] != "client-supplied-id"
    assert headers["x-request-id"] == body["request_id"]


def test_http_rate_limit_has_retry_contract(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.rate_limiter = _RejectingLimiter()
    status, body, headers = _request_with_headers(base_url, "/v1/add", b"not-json")

    assert status == 429
    assert body == {
        "error": "RateLimitedError",
        "message": "too many requests",
        "retryable": True,
        "request_id": body["request_id"],
    }
    assert headers["retry-after"] == "1"
    assert headers["x-request-id"] == body["request_id"]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_message", "retryable"),
    [
        (PermissionDeniedError("write"), 403, "permission denied", False),
        (NotFoundError("memory", "missing"), 404, "resource not found", False),
        (ConflictError("memory", "duplicate"), 409, "resource conflict", False),
        (
            PartialFailureError(
                completed=("ok-1",),
                failed="bad-2",
                retry_action="batch_add",
                message="partial batch failure",
            ),
            409,
            "partial batch failure",
            False,
        ),
        (RateLimitedError("busy"), 429, "too many requests", True),
        (BackendError("backend unavailable"), 503, "service temporarily unavailable", True),
        (HealthCheckError("health failed"), 503, "service temporarily unavailable", True),
        (
            UnsupportedCapabilityError("modality", "video", "PassthroughNormalizer"),
            400,
            "'PassthroughNormalizer' does not support modality 'video'",
            False,
        ),
    ],
)
def test_http_maps_domain_errors_to_stable_statuses(
    http_endpoint,
    error: AgentMemoryError,
    expected_status: int,
    expected_message: str,
    retryable: bool,
) -> None:
    base_url, server = http_endpoint

    def failing_add(*args, **kwargs):
        del args, kwargs
        raise error

    server.api.add = failing_add
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
    )

    assert status == expected_status
    assert body["error"] == type(error).__name__
    assert body["message"] == expected_message
    assert body["retryable"] is retryable
    assert body["request_id"] == headers["x-request-id"]
    if expected_status == 429:
        assert headers["retry-after"] == "1"
    else:
        assert "retry-after" not in headers


def test_http_partial_failure_preserves_batch_retry_contract(http_endpoint) -> None:
    base_url, server = http_endpoint

    def failing_batch_add(*args, **kwargs):
        del args, kwargs
        raise PartialFailureError(
            completed=("ok-1",),
            failed="bad-2",
            retry_action="batch_add",
            message="partial batch failure",
        )

    server.api.batch_add = failing_batch_add
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/batch_add",
        b'{"scope":{"org":"acme","user":"alice"},"items":[{"content":"x"}]}',
    )

    assert status == 409
    assert body == {
        "error": "PartialFailureError",
        "message": "partial batch failure",
        "retryable": False,
        "completed": ["ok-1"],
        "failed": "bad-2",
        "retry_action": "batch_add",
        "request_id": headers["x-request-id"],
    }


def test_http_unexpected_error_is_generic_and_not_retryable(http_endpoint) -> None:
    base_url, server = http_endpoint

    def failing_add(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("Authorization: Bearer secret-token password=secret-password")

    server.api.add = failing_add
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
    )

    assert status == 500
    assert body == {
        "error": "InternalError",
        "message": "internal server error",
        "retryable": False,
        "request_id": body["request_id"],
    }
    assert headers["x-request-id"] == body["request_id"]
    assert "secret-token" not in json.dumps(body)
    assert "secret-password" not in json.dumps(body)


@pytest.mark.parametrize("method", ["add", "search"])
@pytest.mark.parametrize("upstream_status", [429, 400, 500])
def test_http_upstream_error_response(http_endpoint, method: str, upstream_status: int) -> None:
    base_url, server = http_endpoint
    error = upstream_api_error(upstream_status)

    def fail(*args, **kwargs):
        raise error

    setattr(server.api, method, fail)
    payload = (
        {"scope": {"org": "acme", "user": "alice"}, "content": "x"}
        if method == "add"
        else {"context": {"scope": {"org": "acme", "user": "alice"}}, "query": "x"}
    )
    status, body, headers = _request_with_headers(
        base_url, f"/v1/{method}", json.dumps(payload).encode()
    )
    limited = upstream_status == 429
    assert status == (429 if limited else 500)
    assert body == {
        "error": "RateLimitError" if limited else "InternalError",
        "message": "too many requests" if limited else "internal server error",
        "retryable": limited,
        "request_id": headers["x-request-id"],
    }
    assert body["request_id"]
    if limited:
        assert headers["retry-after"] == "1"
    else:
        assert "retry-after" not in headers


def test_http_validation_error_is_redacted(http_endpoint) -> None:
    base_url, server = http_endpoint

    def failing_add(*args, **kwargs):
        del args, kwargs
        raise ValidationError(
            "Authorization: Bearer secret-token token=secret-token "
            "api_key=secret-key password=secret-password "
            "https://user:secret-password@example.com"
        )

    server.api.add = failing_add
    status, body, _ = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"x"}',
    )

    encoded = json.dumps(body)
    assert status == 400
    assert body["error"] == "ValidationError"
    assert body["retryable"] is False
    assert "secret-token" not in encoded
    assert "secret-key" not in encoded
    assert "secret-password" not in encoded
    assert "user:secret-password@example.com" not in encoded


def test_http_audit_detail_contains_response_request_id(
    http_endpoint_with_recording_permission,
) -> None:
    base_url, server = http_endpoint_with_recording_permission
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"scope":{"org":"acme","user":"alice"},"content":"audit"}',
    )

    assert status == 200
    assert isinstance(body, list)
    assert headers["x-request-id"]
    events = server.api.audit(
        {"action": "add"},
        security=internal_context(
            ScopedAuthenticator(Scope(org="acme", user="audit-reader"), role=Role.ROOT)
        ),
    )
    event = next(event for event in events if event.action == "add")
    assert event.detail["request_id"] == headers["x-request-id"]


def test_portal_uses_existing_authentication_and_requires_same_origin(http_endpoint):
    from tests.integration.portal.test_document_portal import Browser, personal_list

    base_url, _ = http_endpoint
    headers = {"Origin": base_url, "Content-Type": "application/json"}
    status, _, _ = _request(base_url, "/portal/api/launch", b"{}", key=None, headers=headers)
    assert status == 401
    status, launch, response_headers = _request_with_headers(
        base_url, "/portal/api/launch", b"{}", key="test-key", headers=headers,
    )
    assert status == 200
    assert response_headers["cache-control"] == "no-store"
    browser = Browser(launch["portal_url"])
    assert browser.call("list", personal_list())[0] == 200
    assert browser.call("list", personal_list(), origin="http://evil.test")[0] == 404
    assert browser.call("admin_get", {})[0] == 404
    status, _, _ = _request(base_url, "/portal/api/list", b"{}", key="test-key", headers=headers)
    assert status == 404
    for path, data, method in (("/healthz", None, "GET"), ("/v1/add", b"{}", "POST")):
        _, _, response_headers = _request_with_headers(base_url, path, data, method=method)
        assert "cache-control" not in response_headers


def test_portal_rejects_dns_rebinding_host(http_endpoint):
    base_url, _ = http_endpoint
    status, _, _ = _request(
        base_url, "/portal/api/launch", b"{}",
        headers={"Host": "evil.test", "Origin": "http://evil.test"},
        key="test-key",
    )
    assert status == 404


def test_portal_session_rejects_changed_authenticated_identity(http_endpoint):
    from tests.integration.portal.test_document_portal import Browser, personal_list

    base_url, server = http_endpoint
    status, launch, _ = _request(
        base_url, "/portal/api/launch", b"{}", key="test-key",
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200
    browser = Browser(launch["portal_url"])
    assert browser.call("list", personal_list())[0] == 200
    server.security_runtime.authenticator.actor.user = "bob"
    assert browser.call("list", personal_list())[0] == 401


def test_portal_real_write_list_search_and_detail(dev_http_endpoint):
    from tests.integration.portal.test_document_portal import (
        Browser,
        memory_content,
        personal_list,
        personal_search,
        record_target,
    )

    base_url, _ = dev_http_endpoint
    scope = {"org": "local", "user": "developer"}
    status, _, _ = _request(
        base_url, "/v1/add", json.dumps({
            "content": "个人记忆检索样本", "scope": scope,
            "system_metadata": {"source_app": "portal-test"},
        }).encode(), key=None,
    )
    assert status == 200
    status, ordinary, _ = _request(
        base_url, "/v1/list", json.dumps({"scope": scope}).encode(), key=None,
    )
    assert status == 200 and ordinary.keys() == {"items", "count"}
    status, launch, _ = _request(
        base_url, "/portal/api/launch", b"{}", key=None,
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200
    browser = Browser(launch["portal_url"])
    status, page = browser.call("list", personal_list())
    assert status == 200 and page["sources"] == ["portal-test"]
    assert page["items"] == ordinary["items"]
    assert memory_content(page["items"][0]) == "个人记忆检索样本"
    status, detail = browser.call("get", record_target(page["items"][0]))
    assert status == 200 and detail["system_metadata"]["source_app"] == "portal-test"
    status, hits = browser.call("search", personal_search("个人记忆"))
    assert status == 200, hits
    assert any("个人记忆检索样本" in hit["item"]["content"] for hit in hits["items"])


def test_portal_lookup_at_capacity_does_not_evict_live_session():
    from jiuwen_memory_entry.portal.sessions import BrowserSessions

    sessions = BrowserSessions()
    grants = [sessions.issue(i) for i in range(32)]
    tokens = [sessions.redeem(grant, embedded=True) for grant in grants]
    assert all(tokens)
    assert sessions.lookup(None, token=tokens[0]) == (True, 0)
    next_grant = sessions.issue(32)
    assert sessions.redeem(grants[0], embedded=True) is None
    assert sessions.lookup(None, token=tokens[0]) == (True, 0)
    new_token = sessions.redeem(next_grant, embedded=True)
    assert sessions.lookup(None, token=new_token) == (True, 32)
    assert sessions.lookup(None, token=tokens[0]) == (False, None)
    assert sessions.lookup(None, token=tokens[1]) == (True, 1)


def test_portal_save_config_retries_latest_saved_version(tmp_path, monkeypatch):
    from jiuwen_memory.api import assemble_managed_server_runtime
    from jiuwen_memory.config.model_runtime import ModelRuntimeSource
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    config = {"llm": {"default": {"target": "echo", "params": {"llm_model": "before"}}}}
    store = SQLiteOverlayStore(tmp_path / "settings.db")
    scope = Scope(org="local", user="developer")

    def authorize(security, operation):
        del operation
        return security.auth.actor == scope

    runtime = assemble_managed_server_runtime(
        config,
        store=store,
        llm_instances=["default"],
        authorize=authorize,
        security_runtime=build_dev_security_runtime(),
    )
    server = HttpServer(load_config([OFFLINE]), runtime, runtime.security_runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    headers = {"Origin": base_url, "Content-Type": "application/json"}
    changes = {"version": 0, "changes": {"llm.default.params.llm_model": "after"}}
    try:
        with monkeypatch.context() as patch:
            fail_apply = Mock(side_effect=RuntimeError("synthetic apply failure"))
            patch.setattr(ModelRuntimeSource, "apply", fail_apply)
            status, result, _ = _request(
                base_url, "/v1/save_config", json.dumps(changes).encode(),
                key=None, headers=headers,
            )
        assert status == 200 and result == {"version": 1, "applied": False}, result
        status, settings, _ = _request(
            base_url, "/v1/read_config", b"{}", key=None, headers=headers
        )
        assert status == 200 and settings["apply_state"] == "apply_failed", settings
        status, applied, _ = _request(
            base_url, "/v1/save_config", b'{"retry":true}', key=None, headers=headers
        )
        assert status == 200 and applied == {"version": 1, "applied": True}, applied
        status, settings, _ = _request(
            base_url, "/v1/read_config", b"{}", key=None, headers=headers
        )
        assert status == 200, settings
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.params.llm_model"]["effective"] == "after"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)


def test_personal_build_binds_one_runtime_and_falls_back_to_read_only(tmp_path):
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    server = HttpServer.build_personal(
        load_config([OFFLINE]),
        llm_instances=["default"],
        authorize=lambda security, operation: True,
        security_runtime=build_dev_security_runtime(),
    )
    try:
        assert server.portal_enabled
        assert server.configuration is None
        other = HttpServer.build_personal(
            load_config([OFFLINE]),
            store=SQLiteOverlayStore(tmp_path / "other.db"),
            llm_instances=["default"],
            authorize=lambda security, operation: True,
            security_runtime=build_dev_security_runtime(),
        )
        try:
            assert other.portal_enabled
            assert other.configuration is not None
            assert server.configuration is None
        finally:
            other.close(wait=True)
    finally:
        server.close(wait=True)


def test_http_build_starts_when_overlay_cannot_be_created(tmp_path, monkeypatch):
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    def denied(self, path):
        raise PermissionError("configuration directory is read-only")

    monkeypatch.setattr(SQLiteOverlayStore, "__init__", denied)
    config = load_config(
        [
            OFFLINE,
            {
                "memory_api": {
                    "globals": {
                        "write_document": True,
                        "watch_document": False,
                        "vector_enabled": False,
                    },
                    "markdown_store": {
                        "default": {"target": "local", "params": {"root": str(tmp_path)}},
                    },
                    "shadow_index": {"default": {"target": "sqlite", "params": {}}},
                    "store_manager": {
                        "default": {
                            "target": "composite",
                            "params": {
                                "domain_stores": {
                                    "default": {
                                        "kv_store": "default",
                                        "shadow_recaller": "shadow",
                                        "preferred_retrieval_pipeline": "recall_and_get_rank",
                                    },
                                },
                            },
                        },
                    },
                    "recaller": {"shadow": {"target": "shadow"}},
                },
            },
        ]
    )
    server = HttpServer.build(with_local_dev_security(config))
    try:
        assert server.portal_enabled
        assert server.configuration is None
    finally:
        server.close(wait=True)


def test_default_server_portal_discovers_personal_scopes(dev_http_endpoint):
    from tests.integration.portal.test_document_portal import (
        memory_content,
        personal_list,
        personal_search,
    )
    base_url, server = dev_http_endpoint
    assert server.portal_enabled
    status, _, _ = _request(
        base_url,
        "/v1/add",
        json.dumps(
            {
                "content": "自动发现的个人记忆",
                "scope": {"org": "local", "user": "developer", "agent": "agent-one"},
            }
        ).encode(),
        key=None,
    )
    assert status == 200
    status, _, _ = _request(
        base_url,
        "/v1/add",
        json.dumps(
            {
                "content": "自动发现的第二个代理记忆",
                "scope": {"org": "local", "user": "developer", "agent": "agent-two"},
            }
        ).encode(),
        key=None,
    )
    assert status == 200
    status, result, _ = _request(
        base_url,
        "/v1/list",
        json.dumps(personal_list()).encode(),
        key=None,
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200, result
    assert any("自动发现的个人记忆" in memory_content(item) for item in result["items"])
    status, hits, _ = _request(
        base_url,
        "/v1/search",
        json.dumps(personal_search("自动发现")).encode(),
        key=None,
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200, hits
    assert hits["scores_comparable"] is False
    assert any("自动发现的个人记忆" in item["item"]["content"] for item in hits["items"])
    assert any("自动发现的第二个代理记忆" in item["item"]["content"] for item in hits["items"])


def test_http_portal_cold_start_accepts_registered_document_plugins(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    code = """
from pathlib import Path
import sys
from jiuwen_memory_entry.core.dev_security import with_local_dev_security
from jiuwen_memory_entry.core.profiles import load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from tests.integration.portal.test_document_portal import document_config

config = with_local_dev_security(load_config([{"memory_api": document_config(Path(sys.argv[1]))}]))
server = HttpServer.build(config)
try:
    assert server.configuration is not None
finally:
    server.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_default_personal_server_http_configuration_without_new_path(tmp_path):
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import _agent_default_config

    memory = _agent_default_config()
    memory["globals"].update({"embedder_dim": 64, "watch_document": False})
    memory["llm"] = {"default": {"target": "echo", "params": {"llm_model": "before"}}}
    memory["extractor"] = {
        "default": {"target": "dynamic_llm", "params": {"llm": "default", "fallback": "legacy"}},
        "legacy": {"target": "keyword", "params": {"chunker": "default"}},
    }
    memory["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    memory["fulltext_store"] = {"default": {"target": "memory"}}
    memory["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    memory["prompts"] = {"extract": {"fact": "before prompt"}}
    config = with_local_dev_security(load_config([{"memory_api": memory}]))
    server = HttpServer.build(config)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    headers = {"Origin": base_url, "Content-Type": "application/json"}
    try:
        status, settings, _ = _request(
            base_url, "/v1/read_config", b"{}", key=None, headers=headers
        )
        assert status == 200, settings
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.params.llm_model"]["writable"]
        assert fields["llm.default.params.llm_api_key"]["writable"]
        assert "prompts.extract.fact" not in fields
        blocked = {"version": 0, "changes": {"prompts.extract.fact": "must not apply"}}
        blocked_status, _, _ = _request(
            base_url,
            "/v1/save_config",
            json.dumps(blocked).encode(),
            key=None,
            headers=headers,
        )
        assert blocked_status == 400
        payload = {
            "version": 0,
            "changes": {
                "llm.default.params.llm_model": "after",
            },
        }
        status, result, _ = _request(
            base_url,
            "/v1/save_config",
            json.dumps(payload).encode(),
            key=None,
            headers=headers,
        )
        assert status == 200 and result == {"version": 1, "applied": True}, result
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)
    restarted = HttpServer.build(config)
    try:
        security = internal_context(restarted.authenticator)
        settings = restarted.configuration.read(security=security)
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.params.llm_model"]["effective"] == "after"
    finally:
        restarted.close(wait=True)

    # 自定义入口安全组合没有选择托管配置，即使旧的文档配置库损坏也可启动。
    overlay = tmp_path / "memory" / ".shadow" / "portal-config.db"
    overlay.write_bytes(b"corrupt database")
    changed_auth = HttpServer.build(config, security_runtime=build_dev_security_runtime())
    try:
        assert changed_auth.configuration is None
        assert overlay.read_bytes() == b"corrupt database"
    finally:
        changed_auth.close(wait=True)


def test_ordinary_file_based_server_ignores_portal_overlay(tmp_path):
    from jiuwen_memory.config.auto_overlay import open_file_overlay

    path = tmp_path / "server.yml"
    path.write_text("profile: offline\n")
    config = replace(load_config([OFFLINE]), config_paths=(str(path),))
    server = HttpServer.build(config)
    try:
        assert server.configuration is None
        assert not list(tmp_path.glob(".server.yml.portal-*.db"))
    finally:
        server.close(wait=True)

    assert open_file_overlay(None, config.config_paths) is not None
    overlay, = tmp_path.glob(".server.yml.portal-*.db")
    overlay.write_bytes(b"corrupt database")
    server = HttpServer.build(config)
    try:
        assert server.configuration is None
        assert overlay.read_bytes() == b"corrupt database"
    finally:
        server.close(wait=True)


def test_file_based_personal_server_config_persists_across_restart(tmp_path):
    path = tmp_path / "server.yml"
    path.write_text("memory_api: {}\n")
    memory = {"llm": {"default": {"target": "echo", "params": {"llm_model": "before"}}}}
    config = with_local_dev_security(
        replace(
            load_config([{"memory_api": memory}]),
            config_paths=(str(path),),
        )
    )
    server = HttpServer.build(config)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_cls())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    headers = {"Content-Type": "application/json"}
    try:
        status, settings, _ = _request(
            base_url,
            "/v1/read_config",
            b"{}",
            key=None,
            headers=headers,
        )
        assert status == 200, settings
        assert settings["available"]
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.params.llm_model"]["writable"]
        assert fields["llm.default.params.llm_api_key"]["writable"]
        payload = {
            "version": settings["version"],
            "changes": {"llm.default.params.llm_model": "after"},
        }
        status, result, _ = _request(
            base_url,
            "/v1/save_config",
            json.dumps(payload).encode(),
            key=None,
            headers=headers,
        )
        assert status == 200, result
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.close(wait=True)
    restarted = HttpServer.build(config)
    try:
        security = internal_context(restarted.authenticator)
        settings = restarted.configuration.read(security=security)
        fields = {field["key"]: field for field in settings["fields"]}
        assert fields["llm.default.params.llm_model"]["effective"] == "after"
    finally:
        restarted.close(wait=True)


def test_portal_add_uses_authenticated_scope_and_rejects_other_users_write_target():
    actor = Scope(org="local", user="alice")
    security = SimpleNamespace(auth=SimpleNamespace(actor=actor))
    api = Mock()
    api.add.return_value = []
    write_scope = Scope(org="local", user="alice")
    payload = {"content": "记忆", "scope": {}}
    invoke_browser_api(api, "add", payload, security=security, write_scope=write_scope)
    api.add.assert_called_once_with(
        "记忆", Scope(org="local", user="alice"), security=security,
        system_metadata={"source_app": "portal"},
    )
    actor.user = "bob"
    with pytest.raises(PermissionDeniedError):
        invoke_browser_api(api, "add", payload, security=security, write_scope=write_scope)
    assert api.add.call_count == 1


@pytest.mark.parametrize("operation", ["get", "update", "delete"])
def test_portal_record_operations_recheck_binding_and_read_permission(operation):
    api = Mock()
    scope = Scope(org="local", user="alice")
    security = SimpleNamespace(auth=SimpleNamespace(actor=scope))

    def call(target):
        payload = {"scope": vars(target), "unit_id": "one"}
        if operation == "update":
            payload["patch"] = {"content": "changed"}
        elif operation == "delete":
            payload = {"selector": {
                "scope": vars(target), "unit_ids": ["one"], "mode": "forget",
            }}
        return invoke_browser_api(api, operation, payload, security=security)

    with pytest.raises(PermissionDeniedError):
        call(Scope(org="other"))
    api.get.assert_not_called()
    api.get.side_effect = PermissionDeniedError()
    with pytest.raises(PermissionDeniedError):
        call(scope)
    api.update.assert_not_called()
    api.delete.assert_not_called()


def test_portal_mutations_preserve_personal_target_and_lifecycle(dev_http_endpoint, monkeypatch):
    from tests.integration.portal.test_document_portal import (
        Browser,
        memory_content,
        personal_list,
        record_target,
    )

    base_url, _ = dev_http_endpoint
    status, launch, _ = _request(
        base_url, "/portal/api/launch", b"{}", key=None,
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200
    call = Browser(launch["portal_url"]).call
    assert call("add", {"content": "forged", "scope": {"user": "other"}})[0] == 400
    status, added = call("add", {"content": "original preference", "scope": {}})
    assert status == 200 and added[0]["system_metadata"]["source_app"] == "portal", added
    assert call("list", {"scope": added[0]["scope"]})[0] == 400
    assert call("list", personal_list())[1]["items"][0]["id"] == added[0]["id"]
    target = record_target(added[0])
    assert call("update", {
        **target, "patch": {"content": "invalid", "mode": "overwrite"},
    })[0] == 400
    with monkeypatch.context() as patch:
        patch.setattr(MemoryQueryService, "candidate_scopes", Mock(
            side_effect=AssertionError("single record operation must not enumerate scopes"),
        ))
        status, corrected = call("update", {**target, "patch": {"content": "corrected preference"}})
        assert status == 200 and corrected["supersedes"] == added[0]["id"], corrected
        assert memory_content(corrected) == "corrected preference"
        selector = {"scope": corrected["scope"], "unit_ids": [corrected["id"]]}
        assert call("delete", {"selector": {**selector, "mode": "purge"}})[0] == 400
        status, forgotten = call("delete", {"selector": {**selector, "mode": "forget"}})
        assert status == 200 and forgotten == [corrected["id"]], forgotten
        assert call("get", record_target(corrected))[1]["lifecycle"] == "forgotten"
    assert call("update", {
        **record_target(corrected), "patch": {"content": "stale correction"},
    })[0] == 409
    assert call("delete", {"selector": {**selector, "mode": "forget"}})[0] == 409
    assert call("list", personal_list())[1]["count"] == 0
    assert call("get", {**target, "scope": {"org": "local", "user": "other"}})[0] == 403


def test_http_portal_browser_launch_reuses_authentication_and_service_port(http_endpoint):
    import asyncio
    from urllib.parse import urlsplit

    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider
    from tests.integration.portal.test_document_portal import Browser, personal_list

    base_url, server = http_endpoint

    async def run():
        provider = AgentMemoryMemoryProvider(
            base_url=base_url, api_key="test-key", portal={"port": 12345}
        )
        assert provider.portal_status["supported"]
        assert not provider.portal_status["available"]
        await provider.initialize()
        try:
            assert not server.portal_sessions.grants
            assert not provider.portal_status["available"]
            url = await provider.open_portal()
            assert len(server.portal_sessions.grants) == 1
            assert provider.portal_status["available"]
            assert urlsplit(url).port == urlsplit(base_url).port
            assert provider.runtime is None
            browser = await asyncio.to_thread(Browser, url)
            status, result = await asyncio.to_thread(browser.call, "list", personal_list())
            assert status == 200, result
            assert provider.portal_url is None
            another_url = await provider.open_portal()
            assert another_url != url
            await asyncio.to_thread(Browser, another_url)
            assert (await asyncio.to_thread(browser.call, "bootstrap", {"token": browser.token}))[
                0
            ] == 401
            assert (
                await asyncio.to_thread(browser.call, "list", personal_list(), origin="http://evil.invalid")
            )[0] == 404
        finally:
            await provider.shutdown()
        assert server.api is not None

    asyncio.run(run())


def test_http_portal_browser_requires_session_even_with_fixed_dev_identity(dev_http_endpoint):
    from urllib.error import HTTPError
    from urllib.request import Request

    from jiuwen_memory_entry.portal.sessions import BROWSER_HEADER
    from tests.integration.portal.test_document_portal import Browser, personal_list

    base_url, server = dev_http_endpoint
    status, launch, _ = _request(
        base_url, "/portal/api/launch",
        json.dumps({"write_target": {"agent": "rail", "space": "", "session": "thread"}}).encode(),
        key=None, headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    assert status == 200
    browser = Browser(launch["portal_url"])
    list_url = f"{base_url}/v1/list"
    request = Request(
        list_url, json.dumps(personal_list()).encode(),
        headers={"Origin": base_url, "Content-Type": "application/json", BROWSER_HEADER: "1"},
    )
    with browser.opener.open(request, timeout=3) as response:
        assert json.load(response)["add_context"] == {"agent": "rail", "agent_locked": True}
    with server.portal_sessions.lock:
        server.portal_sessions.sessions.clear()
    with pytest.raises(HTTPError) as error:
        browser.opener.open(request, timeout=3)
    assert error.value.code == 401
    assert browser.call("list", personal_list())[0] == 401
    without_marker = Request(
        list_url, json.dumps(personal_list()).encode(),
        headers={"Origin": base_url, "Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as error:
        browser.opener.open(without_marker, timeout=3)
    assert error.value.code == 401


def test_embedded_http_portal_binds_authenticated_rail_target(http_endpoint):
    from urllib.parse import parse_qs, urlsplit

    base_url, server = http_endpoint
    parent = "http://localhost:8080"
    server.portal_frame_origins = (parent,)
    headers = {"Origin": base_url, "Content-Type": "application/json"}

    from tests.integration.portal.test_document_portal import personal_list

    def post(action, body, *, key=None, session=None):
        request_headers = dict(headers)
        if session:
            request_headers["X-Memory-Portal-Session"] = session
        prefix = "/portal/api/" if action in {"launch", "bootstrap"} else "/v1/"
        return _request(base_url, prefix + action, json.dumps(body).encode(),
                        key=key, headers=request_headers)

    scope = {"space": "", "agent": "rail", "session": "turn"}
    status, _, _ = post("launch", {"write_target": {**scope, "user": "other"}}, key="test-key")
    assert status == 400
    status, launch, _ = post("launch", {"write_target": scope}, key="test-key")
    assert status == 200
    token = parse_qs(urlsplit(launch["portal_url"]).fragment)["access"][0]
    status, result, _ = post("bootstrap", {"token": token, "embedded": True})
    assert status == 200
    session = result["session_token"]
    assert post("bootstrap", {"token": token, "embedded": True})[0] == 401
    assert post("list", personal_list(), session="invalid")[0] == 401
    assert post("launch", {"write_target": scope}, session=session)[0] == 404
    status, result, _ = post("add", {
        "content": "rail note", "scope": {}, "system_metadata": {"project": "demo"},
    }, session=session)
    assert status == 200, result
    assert result[0]["scope"]["agent"] == "rail"
    assert result[0]["scope"]["session"] == "turn"
    assert result[0]["system_metadata"]["project"] == "demo"
    with _NO_PROXY_OPENER.open(f"{base_url}/portal/", timeout=3) as page:
        assert page.headers["Content-Security-Policy"] == f"frame-ancestors {parent}"
        assert page.headers.get("X-Frame-Options") is None
