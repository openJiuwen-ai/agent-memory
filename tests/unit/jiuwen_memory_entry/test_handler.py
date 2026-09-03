# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import (
    PartialFailureError,
    RateLimitedError,
    UnsupportedCapabilityError,
)
from jiuwen_memory_entry.core import handler
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

pytestmark = pytest.mark.unit


def test_rate_limited_error_preserves_legacy_400_mapping() -> None:
    """审计接口 PR 不应把既有 HTTP 限流响应从 400 隐式改为 429。"""

    class _Api:
        @staticmethod
        def audit(_filters, *, security, limit=100):
            del security, limit
            raise RateLimitedError("rate limit exceeded")

    srv = SimpleNamespace(api=_Api())

    status, body = handler.dispatch(srv, build_legacy_dispatch_request("audit", {}))

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
        build_legacy_dispatch_request(
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
        build_legacy_dispatch_request(
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
