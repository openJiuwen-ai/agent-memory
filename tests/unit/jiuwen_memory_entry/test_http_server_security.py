# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP transport authentication and DTO boundary tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import AuthenticationError
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.types import Action, AuthContext
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


@PermissionProducer.register("http_server_security_recording")
def _build_recording_permission_manager(_config) -> _RecordingPermissionManager:
    manager = _RecordingPermissionManager()
    _RECORDING_PERMISSION_MANAGERS.append(manager)
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
        yield f"http://127.0.0.1:{httpd.server_port}"
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
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=request_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read()), response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers.get_content_type()


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


def test_http_missing_credentials_returns_401(http_endpoint) -> None:
    base_url, _ = http_endpoint
    request = urllib.request.Request(
        f"{base_url}/v1/add",
        data=b'{"target":{"tenant_id":"acme","scope":"alice"},"content":"x"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=3)

    assert exc_info.value.code == 401
    assert json.loads(exc_info.value.read())["request_id"]


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
    status, response = _post(
        http_endpoint_without_security_runtime,
        {"target": {"tenant_id": "acme"}, "content": "must not execute"},
    )

    assert status == 503
    assert response["error"] == "SecurityUnavailable"
    assert response["request_id"]


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
    assert captured["security"] is not None
    assert captured["security"].surface.value == "http"
    assert captured["security"].auth.actor == Scope(org="acme", user="root")


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
    status, body, _ = _request(base_url, "/v1/add", b"{}x")

    assert status == 413
    assert body["error"] == "PayloadTooLarge"
    assert "request_id" not in body


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
