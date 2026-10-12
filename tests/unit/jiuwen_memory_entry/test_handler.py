# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import DisclosureLevel, EvolveMode, Scope
from jiuwen_memory.common.errors import (
    PartialFailureError,
    RateLimitedError,
    UnsupportedCapabilityError,
)
from jiuwen_memory_entry.core import handler
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

pytestmark = pytest.mark.unit


def test_legacy_evolve_uses_v1_flat_mode() -> None:
    calls = []

    class RecordingApi:
        @staticmethod
        def evolve(scope, mode, *, security):
            calls.append((scope, mode, security))
            return "job-legacy"

    srv = SimpleNamespace(api=RecordingApi())
    status, body = handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "evolve", {"tenant_id": "acme", "scope": "alice", "mode": "consolidate"}
        ),
    )

    assert status == 200
    assert body == {
        "ok": True, "op": "evolve", "mode": "consolidate", "job_id": "job-legacy",
    }
    assert len(calls) == 1
    assert calls[0][0] == Scope(org="acme", user="alice")
    assert calls[0][1] is EvolveMode.CONSOLIDATE
    assert calls[0][2].actor == Scope(org="acme", user="alice")


def test_legacy_search_uses_v1_flat_options() -> None:
    calls = []

    class RecordingApi:
        @staticmethod
        def search(query, context, **options):
            calls.append((query, context, options))
            return SimpleNamespace(items=[], trajectory=[])

    srv = SimpleNamespace(api=RecordingApi())
    status, body = handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "search", {"tenant_id": "acme", "scope": "alice", "query": "coffee", "k": 3}
        ),
    )

    assert status == 200
    assert body == {"ok": True, "op": "search", "hits": [], "count": 0}
    assert calls[0][0] == "coffee"
    assert calls[0][1].scope == Scope(org="acme", user="alice")
    assert calls[0][2]["top_k"] == 3
    assert calls[0][2]["disclosure"] is DisclosureLevel.L0

    status, _ = handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "search",
            {
                "tenant_id": "acme",
                "scope": "alice",
                "query": "coffee",
                "disclosure": "l2",
            },
        ),
    )

    assert status == 200
    assert calls[1][2]["disclosure"] is DisclosureLevel.L2


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
