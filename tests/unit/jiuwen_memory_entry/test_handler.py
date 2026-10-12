# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    Credentials,
    DisclosureLevel,
    MemoryUnit,
    Surface,
    UpdateMode,
    build_dev_authenticator,
)
from jiuwen_memory.common.errors import (
    BackendError,
    PartialFailureError,
    RateLimitedError,
    UnsupportedCapabilityError,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory_entry.core import handler
from jiuwen_memory_entry.core.auth_middleware import authenticated
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _dispatch_request(verb: str, payload: dict):
    """布置一次 dispatch：actor 来自 security（测试辅助构造），payload 只作 target。"""
    security = internal_context(ScopedAuthenticator(handler.Scope(org="local", user="developer")))
    return build_legacy_dispatch_request(verb, payload, security=security)


def test_rate_limited_error_preserves_legacy_400_mapping() -> None:
    """审计接口 PR 不应把既有 HTTP 限流响应从 400 隐式改为 429。"""

    class _Api:
        @staticmethod
        def audit(_filters, *, security, limit=100):
            del security, limit
            raise RateLimitedError("rate limit exceeded")

    srv = SimpleNamespace(api=_Api())

    status, body = handler.dispatch(srv, _dispatch_request("audit", {}))

    assert status == 400
    assert body == {"error": "RateLimitedError", "message": "rate limit exceeded"}


def test_partial_failure_error_returns_retry_fields() -> None:
    class _Api:
        @staticmethod
        def delete_space(org, space, *, security, mode=None):
            del org, space, security, mode
            raise PartialFailureError(
                completed=("purge_space",),
                failed="space.delete",
                retry_action="delete_space",
                message="metadata delete failed",
            )

    srv = SimpleNamespace(api=_Api())

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "delete_space",
            {"tenant_id": "acme", "space": "lab"},
        ),
    )

    assert status == 409
    assert body["error"] == "PartialFailureError"
    assert body["completed"] == ["purge_space"]
    assert body["failed"] == "space.delete"
    assert body["retry_action"] == "delete_space"


def test_unsupported_capability_error_maps_to_400() -> None:
    class _Api:
        @staticmethod
        def add(*_args, **_kwargs):
            raise UnsupportedCapabilityError(
                capability="modality",
                value="image",
                component="PassthroughNormalizer",
            )

    srv = SimpleNamespace(api=_Api())

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "add",
            {
                "tenant_id": "org-1",
                "scope": "user-1",
                "modality": "image",
                "content": "file:///photo.jpg",
            },
        ),
    )

    assert status == 400
    assert body["error"] == "UnsupportedCapabilityError"
    assert "modality 'image'" in body["message"]


@pytest.fixture(name="authenticated_request")
def _authenticated_request():
    """参数透传回归始终携带真实认证边界产出的上下文。"""
    with authenticated(
        build_dev_authenticator(), Credentials(), surface=Surface.INTERNAL
    ) as security:

        def build(verb, payload):
            return build_legacy_dispatch_request(
                verb, {"tenant_id": "acme", "scope": "alice", **payload}, security=security
            )

        yield build


@pytest.mark.parametrize("value", [None, "l0", "l1", "l2", "adaptive"])
def test_search_forwards_upstream_disclosure_and_authenticated_context(
    value, authenticated_request
) -> None:
    captured = {}

    def search(query, context, **kwargs):
        captured.update(query=query, context=context, **kwargs)
        return SimpleNamespace(items=[], trajectory=[])

    payload = {"query": "hello"}
    if value is not None:
        payload["disclosure"] = value
    request = authenticated_request("search", payload)

    status, body = handler.dispatch(SimpleNamespace(api=SimpleNamespace(search=search)), request)

    assert status == 200
    assert body["hits"] == []
    assert captured["disclosure"] is DisclosureLevel(value or "l0")
    assert captured["security"] is request.security
    assert captured["security"].actor.org == "local"
    assert captured["context"].scope.org == "acme"


@pytest.mark.parametrize("mode", [None, "supersede", "overwrite"])
def test_update_forwards_upstream_temporal_patch_and_authenticated_context(
    mode, authenticated_request
) -> None:
    captured = {}

    def update(unit_id, scope, patch, *, security):
        captured.update(unit_id=unit_id, scope=scope, patch=patch, security=security)
        return MemoryUnit(id=unit_id, scope=scope)

    payload = {
        "item_id": "u1",
        "content": "updated",
        "t_valid": "2026-10-01T00:00:00+00:00",
        "t_invalid": "2026-11-01T00:00:00+00:00",
    }
    if mode is not None:
        payload["mode"] = mode
    request = authenticated_request("update", payload)

    status, body = handler.dispatch(SimpleNamespace(api=SimpleNamespace(update=update)), request)

    assert status == 200
    assert body["item"]["item_id"] == "u1"
    assert captured["patch"].mode is UpdateMode(mode or "supersede")
    assert captured["patch"].t_valid == datetime.fromisoformat(payload["t_valid"])
    assert captured["patch"].t_invalid == datetime.fromisoformat(payload["t_invalid"])
    assert captured["security"] is request.security
    assert captured["scope"].org == "acme"
    assert captured["security"].actor.org == "local"


@pytest.mark.parametrize(
    ("verb", "payload"),
    [
        ("search", {"query": "hello", "disclosure": "invalid"}),
        ("update", {"item_id": "u1", "t_valid": "invalid"}),
        ("update", {"item_id": "u1", "t_invalid": "invalid"}),
        ("update", {"item_id": "u1", "mode": "invalid"}),
    ],
)
def test_invalid_upstream_parameters_fail_before_api_call(
    verb, payload, authenticated_request
) -> None:
    status, body = handler.dispatch(
        SimpleNamespace(api=SimpleNamespace()), authenticated_request(verb, payload)
    )

    assert status == 400
    assert body["error"] == "ValidationError"


def test_backend_error_maps_to_503_not_400_or_500() -> None:
    """授权依赖故障是服务端依赖不可用，不是调用方参数错误。"""

    class _Api:
        @staticmethod
        def search(_query, _ctx, *, security, **_kwargs):
            del security
            raise BackendError("store down")

    srv = SimpleNamespace(api=_Api())
    request = _dispatch_request("search", {"query": "x"})

    status, body = handler.dispatch(srv, request)

    assert status == 503
    assert body == {"error": "BackendError", "message": "store down"}
