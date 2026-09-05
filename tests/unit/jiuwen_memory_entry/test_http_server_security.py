# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP transport authentication and DTO boundary tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

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
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    RequestSecurityContext,
    get_current,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.permission import PermissionProducer
from jiuwen_memory.control.permission_impl.allow_all_permission_manager import (
    AllowAllPermissionManager,
)
from jiuwen_memory.control.types import PermissionContext
from jiuwen_memory_entry.core.dispatch_request import DispatchRequest
from jiuwen_memory_entry.core.profiles import OFFLINE, load_config
from jiuwen_memory_entry.http_server.__main__ import HttpServer

pytestmark = pytest.mark.unit


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


def _post(base_url: str, body: object, *, key: str = "test-key") -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/add",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
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
) -> tuple[int, dict, str]:
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
) -> tuple[int, dict, dict[str, str]]:
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), {
                key.lower(): value for key, value in response.headers.items()
            }
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), {
            key.lower(): value for key, value in exc.headers.items()
        }


def test_http_uses_authenticated_actor_and_nested_target(http_endpoint) -> None:
    base_url, server = http_endpoint
    status, body = _post(
        base_url,
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "hello"},
    )

    assert status == 200, body
    assert body["request_id"]
    actor = Scope(org="acme", user="alice")
    items = server.api.list(
        Scope(org="acme", user="alice"), security=legacy_request_context(actor)
    ).items
    assert len(items) == 1
    assert items[0].scope == Scope(org="acme", user="alice")


def test_http_rejects_actor_claim_before_business_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    status, body = _post(
        base_url,
        {
            "target": {"tenant_id": "acme", "scope": "alice"},
            "content": "must not write",
            "actor_scope": "root",
        },
    )

    assert status == 400
    assert "reserved" in body["message"]
    assert body["request_id"]
    assert (
        server.api.list(
            Scope(org="acme", user="alice"),
            security=legacy_request_context(Scope(org="acme", user="alice")),
        ).items
        == []
    )


def test_http_rejects_nested_identity_claim_before_business_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def dispatch(_request):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body = _post(
        base_url,
        {
            "target": {"tenant_id": "acme", "scope": "alice", "identity": "root"},
            "content": "must not write",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert "reserved" in body["message"]
    assert body["request_id"]
    assert called is False


def test_http_missing_credentials_returns_401(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def dispatch(_request):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, response, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
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
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
        key=None,
    )

    assert status == 401
    assert audit.events[-1].action == "authenticate"
    assert audit.events[-1].detail["request_id"] == body["request_id"]


def test_http_invalid_credentials_returns_401_before_dto(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.authenticator.fail = True
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body, _ = _request(base_url, "/v1/add", b"not-json", key="bad")

    assert status == 401
    assert body["error"] == "AuthenticationError"
    assert body["request_id"]
    assert called is False


def test_http_unknown_field_is_400_and_does_not_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body = _post(
        base_url,
        {"target": {"tenant_id": "acme"}, "content": "x", "unexpected": True},
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

    def dispatch(_request):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, response = _post(
        base_url,
        {"target": {"tenant_id": "acme"}, "content": "must not execute"},
    )

    assert status == 503
    assert response["error"] == "SecurityUnavailable"
    assert response["request_id"]
    assert response["retryable"] is False
    assert called is False


def test_http_authenticated_actor_is_independent_from_target(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.authenticator.actor = Scope(org="acme", user="root")
    captured: dict[str, object] = {}

    def dispatch(request):
        assert isinstance(request, DispatchRequest)
        captured["identity"] = request.actor
        captured["target"] = request.target
        captured["security"] = request.security
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body = _post(
        base_url,
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "x"},
    )

    assert status == 200, body
    assert captured["identity"] == Scope(org="acme", user="root")
    assert captured["target"] == Scope(org="acme", user="alice")
    security = captured["security"]
    assert isinstance(security, RequestSecurityContext)
    assert security.surface.value == "http"
    assert security.auth.actor == Scope(org="acme", user="root")
    assert security.request_id == body["request_id"]


def test_http_video_request_preserves_source_and_uri_for_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    captured: dict[str, object] = {}

    def dispatch(request):
        captured["request"] = request
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body = _post(
        base_url,
        {
            "target": {"tenant_id": "acme", "scope": "alice"},
            "content": "video ingest",
            "source": "video",
            "uri": "file:///tmp/demo.mp4",
        },
    )

    assert status == 200, body
    request = captured["request"]
    assert isinstance(request, DispatchRequest)
    assert dict(request.payload) == {
        "content": "video ingest",
        "source": "video",
        "uri": "file:///tmp/demo.mp4",
    }


def test_http_delete_space_request_preserves_mode_for_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    captured: dict[str, object] = {}

    def dispatch(request):
        captured["request"] = request
        return 200, {"ok": True}

    server.dispatch = dispatch
    status, body, _ = _request(
        base_url,
        "/v1/delete_space",
        json.dumps({"target": {"tenant_id": "acme", "space": "product"}, "mode": "purge"}).encode(),
    )

    assert status == 200, body
    request = captured["request"]
    assert isinstance(request, DispatchRequest)
    assert request.target == Scope(org="acme", space="product")
    assert dict(request.payload) == {"mode": "purge"}


def test_http_real_dispatch_keeps_actor_and_target_separate_in_permission_and_audit(
    http_endpoint_with_recording_permission,
) -> None:
    base_url, server = http_endpoint_with_recording_permission
    actor = Scope(org="acme", user="root")
    target = Scope(org="acme", user="alice")
    server.security_runtime.authenticator.actor = actor

    status, body = _post(
        base_url,
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "hello"},
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
            "target": {"tenant_id": "acme", "scope": "alice"},
            "content": "must be denied",
            "actor_scope": "forged",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert _DENYING_PERMISSION_MANAGERS[-1].checks == []

    status, body = _post(
        base_url,
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "must be denied"},
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

    def failing_dispatch(_request):
        raise RuntimeError(
            "Authorization: Bearer secret-token password=secret-password "
            "https://user:secret-password@example.com"
        )

    server.dispatch = failing_dispatch
    with caplog.at_level("ERROR", logger="agent-memory.server"):
        status, body, _ = _request_with_headers(
            base_url,
            "/v1/add",
            b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
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

    def dispatch(request):
        assert isinstance(request, DispatchRequest)
        security = request.security
        assert security is not None
        observed.append((request.actor, security.actor))
        return 200, {"ok": True}

    server.dispatch = dispatch
    for scope in ("target-a", "target-b"):
        status, body = _post(
            base_url,
            {"target": {"tenant_id": "acme", "scope": scope}, "content": "x"},
        )
        assert status == 200, body

    assert observed == [(expected[0], expected[0]), (expected[1], expected[1])]


def test_http_clears_auth_context_on_all_request_outcomes(http_endpoint, monkeypatch) -> None:
    base_url, server = http_endpoint
    from jiuwen_memory_entry.http_server import __main__ as http_server_module

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
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "x"},
    )
    assert success_status == 200, success_body

    dto_status, dto_body = _post(
        base_url,
        {
            "target": {"tenant_id": "acme", "scope": "alice"},
            "content": "x",
            "actor_scope": "forged",
        },
    )
    assert dto_status == 400, dto_body

    def failing_dispatch(_request):
        raise RuntimeError("business failure")

    server.dispatch = failing_dispatch
    error_status, error_body = _post(
        base_url,
        {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "x"},
    )
    assert error_status == 500, error_body

    server.security_runtime.authenticator.fail = True
    auth_status, auth_body, _ = _request(base_url, "/v1/add", b"not-json", key="bad")
    assert auth_status == 401, auth_body

    assert contexts_after_exit == [None, None, None, None]


def test_http_rate_limit_returns_429_before_dto_or_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    server.security_runtime.rate_limiter = _RejectingLimiter()
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
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


def test_http_malformed_json_is_400_and_does_not_dispatch(http_endpoint) -> None:
    base_url, server = http_endpoint
    called = False

    def dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return 200, {"ok": True}

    server.dispatch = dispatch
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
    with urllib.request.urlopen(request, timeout=3) as response:
        body = json.loads(response.read())
        assert response.status == 200
        assert response.headers.get_content_type() == "application/json"
        assert response.headers["X-Request-ID"] == body["request_id"]
    assert body["status"] == "ok"


def test_http_request_ids_are_unique(http_endpoint) -> None:
    base_url, _ = http_endpoint
    ids = []
    for _ in range(2):
        status, body = _post(
            base_url,
            {"target": {"tenant_id": "acme", "scope": "alice"}, "content": "x"},
        )
        assert status == 200
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

    def failing_dispatch(_request):
        raise error

    server.dispatch = failing_dispatch
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
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

    def failing_dispatch(_request):
        raise PartialFailureError(
            completed=("ok-1",),
            failed="bad-2",
            retry_action="batch_add",
            message="partial batch failure",
        )

    server.dispatch = failing_dispatch
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/batch_add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"items":[{"content":"x"}]}',
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

    def failing_dispatch(_request):
        raise RuntimeError("Authorization: Bearer secret-token password=secret-password")

    server.dispatch = failing_dispatch
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
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


def test_http_unknown_dispatch_error_name_is_generic_server_failure(http_endpoint) -> None:
    base_url, server = http_endpoint

    def unknown_dispatch(_request):
        return 500, {"error": "UnexpectedCustomFailure", "message": "token=secret-token"}

    server.dispatch = unknown_dispatch
    status, body, headers = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
    )

    assert status == 500
    assert body["error"] == "InternalError"
    assert body["message"] == "internal server error"
    assert body["retryable"] is False
    assert body["request_id"] == headers["x-request-id"]
    assert "secret-token" not in json.dumps(body)


def test_http_validation_error_is_redacted(http_endpoint) -> None:
    base_url, server = http_endpoint

    def failing_dispatch(_request):
        raise ValidationError(
            "Authorization: Bearer secret-token token=secret-token "
            "api_key=secret-key password=secret-password "
            "https://user:secret-password@example.com"
        )

    server.dispatch = failing_dispatch
    status, body, _ = _request_with_headers(
        base_url,
        "/v1/add",
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
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
        b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"audit"}',
    )

    assert status == 200
    assert body["request_id"] == headers["x-request-id"]
    events = server.api.audit(
        {"action": "add"},
        security=legacy_request_context(Scope(org="acme", user="alice")),
    )
    event = next(event for event in events if event.action == "add")
    assert event.detail["request_id"] == body["request_id"]
