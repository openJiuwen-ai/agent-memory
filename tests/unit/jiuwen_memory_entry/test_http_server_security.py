# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP transport authentication and API contract boundary tests."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

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
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    get_current,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (
    AllowAllPermissionManager,
)
from jiuwen_memory.control.types import PermissionContext
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server import __main__ as http_server_module
from jiuwen_memory_entry.http_server.__main__ import HttpServer
from jiuwen_memory_entry.http_server.dev_security import build_dev_security_runtime

pytestmark = pytest.mark.unit
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Authenticator:
    def __init__(self, actor: Scope | None = None, *, fail: bool = False) -> None:
        self.actor = actor or Scope(org="acme", user="alice")
        self.fail = fail

    def authenticate(self, credentials):
        if self.fail or not credentials.api_key:
            raise AuthenticationError("authentication failed")
        return AuthContext(actor=self.actor, auth_method="test")

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
    server = HttpServer.build(
        load_config([OFFLINE]), security_runtime=build_dev_security_runtime()
    )
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
def http_endpoint_with_recording_permission():
    _RECORDING_PERMISSION_MANAGERS.clear()
    runtime = SimpleNamespace(
        authenticator=_Authenticator(),
        rate_limiter=None,
        workload_guard=None,
        audit=None,
    )
    config = load_config(
        [
            OFFLINE,
            {
                "memory_api": {
                    "permission": {"default": "http_server_security_recording"}
                }
            },
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
            return response.status, json.loads(response.read()), {
                key.lower(): value for key, value in response.headers.items()
            }
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), {
            key.lower(): value for key, value in exc.headers.items()
        }


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
        Scope(org="acme", user="alice"), security=legacy_request_context(actor)
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
            security=legacy_request_context(Scope(org="acme", user="alice")),
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


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_dev_authentication_rejects_non_loopback_binding(host, binding_httpd) -> None:
    server = HttpServer.build(
        load_config([OFFLINE]), security_runtime=build_dev_security_runtime()
    )
    with pytest.raises(ValidationError, match="loopback"):
        server.serve(host, 8137)

    assert binding_httpd == [], "unsafe binding must be rejected before creating a socket"


def test_dev_authentication_cli_rejects_non_loopback_binding(monkeypatch, binding_httpd) -> None:
    monkeypatch.delenv("JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK", raising=False)

    result = http_server_module.main(
        ["--auth-mode", "dev", "--host", "0.0.0.0", "--port", "8137"]
    )

    assert result == 2
    assert binding_httpd == []


def test_dev_authentication_allows_explicit_container_binding(binding_httpd, caplog) -> None:
    server = HttpServer.build(
        load_config([OFFLINE]), security_runtime=build_dev_security_runtime()
    )

    with caplog.at_level(logging.WARNING, logger="agent-memory.server"):
        server.serve("0.0.0.0", 8137, allow_dev_non_loopback=True)

    assert binding_httpd[0].address == ("0.0.0.0", 8137)
    assert binding_httpd[0].served is True
    assert binding_httpd[0].closed is True
    assert "development authentication is listening on non-loopback host" in caplog.text


def test_dev_binding_environment_override_warns(monkeypatch, binding_httpd, caplog) -> None:
    monkeypatch.setenv("JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK", "true")

    with caplog.at_level(logging.WARNING, logger="agent-memory.server"):
        result = http_server_module.main(
            ["--auth-mode", "dev", "--host", "0.0.0.0", "--port", "8137"]
        )

    assert result == 0
    assert binding_httpd[0].served is True
    assert binding_httpd[0].closed is True
    assert "the deployment boundary must prevent remote access" in caplog.text


@pytest.mark.parametrize("allow_override", [False, True])
@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_third_party_binding_error_does_not_suggest_dev_override(
    host, allow_override, binding_httpd
) -> None:
    runtime = SimpleNamespace(authenticator=_LoopbackAuthenticator())
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    with pytest.raises(ValidationError, match="authenticator.*third_party.*loopback") as error:
        server.serve(host, 8137, allow_dev_non_loopback=allow_override)

    assert "development authentication" not in str(error.value)
    assert "JIUWEN_MEMORY_HTTP_ALLOW_DEV_AUTH_NON_LOOPBACK" not in str(error.value)
    assert binding_httpd == []


def test_remote_capable_authenticator_binds_without_dev_override(binding_httpd) -> None:
    runtime = SimpleNamespace(authenticator=_RemoteAuthenticator())
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    server.serve("0.0.0.0", 8137)

    assert binding_httpd[0].served is True
    assert binding_httpd[0].closed is True


def test_binding_policy_denial_is_not_overridden_by_dev_flag(binding_httpd) -> None:
    policy = _RejectingBindingPolicy()
    runtime = SimpleNamespace(
        authenticator=build_dev_security_runtime().authenticator,
        binding_policy=policy,
    )
    server = HttpServer.build(load_config([OFFLINE]), security_runtime=runtime)

    with pytest.raises(ValidationError, match="binding policy rejected host"):
        server.serve("0.0.0.0", 8137, allow_dev_non_loopback=True)

    assert policy.calls == [("0.0.0.0", True)]
    assert binding_httpd == []


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


def test_http_api_call_keeps_actor_and_target_separate_in_permission_and_audit(
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
    assert _RECORDING_PERMISSION_MANAGERS[-1].checks[-1] == (actor, target, Action.WRITE)
    events = server.api.audit({"action": "add"}, security=legacy_request_context(actor))
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
    assert _DENYING_PERMISSION_MANAGERS[-1].checks[-1] == (actor, target, Action.WRITE)
    events = server.api.audit(
        {"action": "add", "decision": "deny"},
        security=legacy_request_context(actor),
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
        security=legacy_request_context(Scope(org="acme", user="alice")),
    )
    event = next(event for event in events if event.action == "add")
    assert event.detail["request_id"] == headers["x-request-id"]
