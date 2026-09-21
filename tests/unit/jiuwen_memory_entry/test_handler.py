# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    PartialFailureError,
    RateLimitedError,
    UnsupportedCapabilityError,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory_entry.core import handler
from jiuwen_memory_entry.core.legacy_request_adapter import (
    build_legacy_dispatch_request,
)
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
