# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from types import SimpleNamespace

import pytest

from jiuwen_memory.api import EvolveMode, Scope
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


class _RecordingEvolveApi:
    """记录 evolve 调用参数的最小 API 桩（MCP/legacy dispatch 路径）。"""

    def __init__(self, job_id: str | None) -> None:
        self.job_id = job_id
        self.calls: list[dict] = []

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel=None,
        *,
        candidate=None,
        dreaming=None,
        interval=0,
        security=None,
    ):
        del channel, security
        self.calls.append(
            {
                "scope": scope,
                "mode": mode,
                "candidate": candidate,
                "dreaming": dreaming,
                "interval": interval,
            }
        )
        return self.job_id


def test_evolve_dreaming_three_states_pass_through() -> None:
    """MCP/legacy dispatch：dreaming 三态 + candidate/interval 透传到 API 层。"""
    api = _RecordingEvolveApi(job_id="job-1")
    srv = SimpleNamespace(api=api)

    # 注册（dreaming=true）：interval 与候选源 DSL 一并下发。
    status, body = handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "evolve",
            {
                "tenant_id": "acme",
                "scope": "alice",
                "mode": "forget",
                "dreaming": True,
                "interval": 86400,
                "candidate": {"type": "predicate", "window": 3600},
            },
        ),
    )
    assert status == 200
    assert body == {"ok": True, "op": "evolve", "mode": "forget", "job_id": "job-1"}
    assert api.calls[0]["scope"] == Scope(org="acme", user="alice")
    assert api.calls[0]["mode"] == EvolveMode.FORGET
    assert api.calls[0]["dreaming"] is True
    assert api.calls[0]["interval"] == 86400
    assert api.calls[0]["candidate"] == {"type": "predicate", "window": 3600}

    # 注销（dreaming=false）：恒返回 None，API 收到 False。
    api.job_id = None
    status, body = handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "evolve",
            {"tenant_id": "acme", "scope": "alice", "mode": "forget", "dreaming": False},
        ),
    )
    assert status == 200
    assert body["job_id"] is None
    assert api.calls[1]["dreaming"] is False
    assert api.calls[1]["interval"] == 0, "未传 interval 时保持缺省 0，不误报注册态"

    # 立即执行（缺省）：dreaming 为 None，candidate 不传。
    handler.dispatch(
        srv,
        build_legacy_dispatch_request(
            "evolve", {"tenant_id": "acme", "scope": "alice", "mode": "extract"}
        ),
    )
    assert api.calls[2]["dreaming"] is None
    assert api.calls[2]["candidate"] is None


def test_evolve_dreaming_field_type_validation() -> None:
    """畸形类型在 dispatch 边界拒绝（400），不下沉到 API 层。"""
    srv = SimpleNamespace(api=_RecordingEvolveApi(job_id="job-1"))

    for field, value in (
        ("dreaming", "yes"),
        ("dreaming", 1),
        ("candidate", [1, 2]),
        ("interval", "abc"),
        ("interval", -1),
    ):
        status, body = handler.dispatch(
            srv,
            build_legacy_dispatch_request(
                "evolve",
                {"tenant_id": "acme", "scope": "alice", "mode": "extract", field: value},
            ),
        )
        assert status == 400, f"{field}={value!r} 应在边界拒绝"
        assert body["error"] == "ValidationError"
