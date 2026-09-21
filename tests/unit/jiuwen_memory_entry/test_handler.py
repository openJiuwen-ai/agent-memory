# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Generic handler 的既有错误映射回归测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    Channel,
    Credentials,
    DeleteMode,
    DisclosureLevel,
    EvolveMode,
    MemoryTier,
    MemoryUnit,
    Scope,
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
        del security
        self.calls.append(
            {
                "scope": scope,
                "mode": mode,
                "channel": channel,
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
        _dispatch_request(
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
        _dispatch_request(
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
        _dispatch_request("evolve", {"tenant_id": "acme", "scope": "alice", "mode": "extract"}),
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
            _dispatch_request(
                "evolve",
                {"tenant_id": "acme", "scope": "alice", "mode": "extract", field: value},
            ),
        )
        assert status == 400, f"{field}={value!r} 应在边界拒绝"
        assert body["error"] == "ValidationError"


class _RecordingSearchApi:
    """记录 search 关键字参数的最小 API 桩（legacy dispatch 路径）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(
        self,
        query,
        context,
        *,
        security,
        filters=None,
        as_of=None,
        top_k=10,
        disclosure=None,
        with_trajectory=False,
    ):
        del query, context, security, filters, disclosure, with_trajectory
        self.calls.append({"as_of": as_of, "top_k": top_k})
        return SimpleNamespace(items=[], trajectory=[])


def test_search_forwards_as_of() -> None:
    """as_of 必须透传：漏传时点查询会静默返回“当前状态”而非历史版本。"""
    api = _RecordingSearchApi()
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "search",
            {
                "tenant_id": "acme",
                "scope": "alice",
                "query": "hello",
                "as_of": "2021-05-01T00:00:00+00:00",
            },
        ),
    )
    assert status == 200
    assert body["ok"] is True
    assert api.calls[0]["as_of"] == datetime(2021, 5, 1, tzinfo=UTC)

    # 未传 as_of：保持 None（缺省 = 当前状态），既有行为不变
    handler.dispatch(
        srv,
        _dispatch_request("search", {"tenant_id": "acme", "scope": "alice", "query": "hello"}),
    )
    assert api.calls[1]["as_of"] is None


def test_search_rejects_bad_as_of() -> None:
    """非 ISO 8601 的 as_of 在边界返回 400，不下发到 API 层。"""
    api = _RecordingSearchApi()
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "search",
            {"tenant_id": "acme", "scope": "alice", "query": "q", "as_of": "yesterday"},
        ),
    )
    assert status == 400
    assert body["error"] == "ValidationError"
    assert api.calls == []


def test_search_forwards_top_k() -> None:
    """k 透传给 API 层，缺省回落 10。"""
    api = _RecordingSearchApi()
    srv = SimpleNamespace(api=api)

    status, _body = handler.dispatch(
        srv,
        _dispatch_request("search", {"tenant_id": "acme", "scope": "alice", "query": "q", "k": 3}),
    )
    assert status == 200
    assert api.calls[-1]["top_k"] == 3

    handler.dispatch(
        srv,
        _dispatch_request("search", {"tenant_id": "acme", "scope": "alice", "query": "q"}),
    )
    assert api.calls[-1]["top_k"] == 10


def test_search_rejects_non_positive_k() -> None:
    """
    k 必须是正整数：非法值在边界返回 400，不下发到 API 层。

    基线 a456a87 用 _parse_positive_int 收紧了 k。冲突解决若把它退回
    int(payload.get("k", 10))，k=0 会被静默放行，而 k="abc" 会抛裸
    ValueError 变成 500。基线与本 PR 两边都没有测试覆盖这条。
    """
    api = _RecordingSearchApi()
    srv = SimpleNamespace(api=api)

    for bad in (0, -5, "abc", "0"):
        status, body = handler.dispatch(
            srv,
            _dispatch_request(
                "search",
                {"tenant_id": "acme", "scope": "alice", "query": "q", "k": bad},
            ),
        )
        assert status == 400, f"k={bad!r}"
        assert body["error"] == "ValidationError"

    # 非法值一个都没下发到 API 层
    assert api.calls == []

    # 显式 null 回落 default=10，仍然成功
    status, _body = handler.dispatch(
        srv,
        _dispatch_request(
            "search", {"tenant_id": "acme", "scope": "alice", "query": "q", "k": None}
        ),
    )
    assert status == 200
    assert api.calls[-1]["top_k"] == 10


class _RecordingDeleteApi:
    """记录 DeleteSelector 的最小 API 桩（legacy dispatch 路径）。"""

    def __init__(self) -> None:
        self.selectors: list = []

    def delete(self, selector, *, security):
        del security
        self.selectors.append(selector)
        return list(selector.unit_ids)


def test_delete_honors_mode() -> None:
    """mode 是一等参数：可恢复的 archive 不得静默降级为不可恢复的 forget。"""
    api = _RecordingDeleteApi()
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "delete",
            {"tenant_id": "acme", "scope": "alice", "item_id": "u1", "mode": "archive"},
        ),
    )
    assert status == 200
    assert body["ok"] is True
    assert api.selectors[0].mode == DeleteMode.ARCHIVE

    # 旧的硬删布尔保持兼容
    handler.dispatch(
        srv,
        _dispatch_request(
            "delete", {"tenant_id": "acme", "scope": "alice", "item_id": "u1", "hard": True}
        ),
    )
    assert api.selectors[1].mode == DeleteMode.PURGE

    handler.dispatch(
        srv,
        _dispatch_request("delete", {"tenant_id": "acme", "scope": "alice", "item_id": "u1"}),
    )
    assert api.selectors[2].mode == DeleteMode.FORGET


def test_delete_hard_is_parsed_boolean_not_truthy() -> None:
    """
    ``hard`` 走 ``_bool_value``：字符串 "false" 不得触发 PURGE。

    冲突解决时若把 ``_bool_value`` 退回 ``payload.get("hard")`` 的真值判断，
    非空字符串 ``"false"`` 会被当成硬删，把可恢复的 FORGET 静默升级成不可
    恢复的 PURGE，且不报任何错。
    """
    api = _RecordingDeleteApi()
    srv = SimpleNamespace(api=api)

    for hard, expected in (
        (False, DeleteMode.FORGET),
        (True, DeleteMode.PURGE),
        ("false", DeleteMode.FORGET),
        ("0", DeleteMode.FORGET),
        ("true", DeleteMode.PURGE),
        ("yes", DeleteMode.PURGE),
    ):
        status, body = handler.dispatch(
            srv,
            _dispatch_request(
                "delete",
                {"tenant_id": "acme", "scope": "alice", "item_id": "u1", "hard": hard},
            ),
        )
        assert status == 200
        assert body["ok"] is True
        assert api.selectors[-1].mode == expected, f"hard={hard!r}"


def test_delete_mode_takes_priority_over_hard() -> None:
    """``mode`` 是一等参数，显式给出时覆盖 ``hard``。"""
    api = _RecordingDeleteApi()
    srv = SimpleNamespace(api=api)

    status, _body = handler.dispatch(
        srv,
        _dispatch_request(
            "delete",
            {
                "tenant_id": "acme",
                "scope": "alice",
                "item_id": "u1",
                "mode": "archive",
                "hard": True,
            },
        ),
    )
    assert status == 200
    assert api.selectors[-1].mode == DeleteMode.ARCHIVE


def test_delete_rejects_unknown_mode() -> None:
    """非法 mode 在边界返回 400，不下发到 API 层。"""
    api = _RecordingDeleteApi()
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "delete",
            {"tenant_id": "acme", "scope": "alice", "item_id": "u1", "mode": "shred"},
        ),
    )
    assert status == 400
    assert body["error"] == "ValidationError"
    assert api.selectors == []


class _RecordingUpdateApi:
    """记录 MemoryPatch 的最小 API 桩（legacy dispatch 路径）。"""

    def __init__(self) -> None:
        self.patches: list = []

    def update(self, unit_id, scope, patch, *, security):
        del scope, security
        self.patches.append(patch)
        return SimpleNamespace(
            id=unit_id,
            content="c",
            tags=[],
            tier=patch.tier or MemoryTier.EPISODIC,
            lifecycle=SimpleNamespace(value="active"),
            assets=[],
            system_metadata={},
            user_metadata={},
        )


def test_update_forwards_tier() -> None:
    """tier 漏传会让“改分类”的请求 200 成功却没有任何效果。"""
    api = _RecordingUpdateApi()
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "update",
            {"tenant_id": "acme", "scope": "alice", "item_id": "u1", "tier": "semantic"},
        ),
    )
    assert status == 200
    assert body["item"]["tier"] == "semantic"
    assert api.patches[0].tier == MemoryTier.SEMANTIC

    handler.dispatch(
        srv,
        _dispatch_request("update", {"tenant_id": "acme", "scope": "alice", "item_id": "u1"}),
    )
    assert api.patches[1].tier is None


def test_evolve_forwards_channel() -> None:
    """channel 是 API 契约的一部分，漏传会静默退化成 BACKGROUND。"""
    api = _RecordingEvolveApi(job_id="job-1")
    srv = SimpleNamespace(api=api)

    status, body = handler.dispatch(
        srv,
        _dispatch_request(
            "evolve",
            {"tenant_id": "acme", "scope": "alice", "mode": "extract", "channel": "hot"},
        ),
    )
    assert status == 200
    assert body["job_id"] == "job-1"
    assert api.calls[0]["channel"] == Channel.HOT

    handler.dispatch(
        srv,
        _dispatch_request("evolve", {"tenant_id": "acme", "scope": "alice", "mode": "extract"}),
    )
    assert api.calls[1]["channel"] == Channel.BACKGROUND


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
