# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Legacy dispatch 入口参数完整性回归测试（#221 / #226 与其余静默丢参字段）。

覆盖 PR #246 之外的漏传：布尔参数真值判断（#221）、枚举非法值绕过错误映射（#226）、
以及 space owner / 成员两轴角色 / 成员时间 / 空容器 patch / 删除选择器 /
grant actions / 视频 occurred_at / 整数与对象类型校验。
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    Action,
    DeleteMode,
    EvolveMode,
    Modality,
    Scope,
    SpaceContentRole,
    SpaceGovernanceRole,
    SpaceInfo,
)
from jiuwen_memory_entry.core import handler
from jiuwen_memory_entry.core.legacy_request_adapter import build_legacy_dispatch_request

pytestmark = pytest.mark.unit

_TARGET = {"tenant_id": "acme", "scope": "alice"}


def _dispatch(api, verb, payload):
    return handler.dispatch(SimpleNamespace(api=api), build_legacy_dispatch_request(verb, payload))


# --- #221 布尔参数 ----------------------------------------------------------- #


class _RecordingDeleteApi:
    def __init__(self) -> None:
        self.selectors: list = []

    def delete(self, selector, *, security):
        del security
        self.selectors.append(selector)
        return list(selector.unit_ids)


@pytest.mark.parametrize(
    ("hard", "expected"),
    [
        (False, DeleteMode.FORGET),
        (True, DeleteMode.PURGE),
        ("false", DeleteMode.FORGET),
        ("0", DeleteMode.FORGET),
        ("no", DeleteMode.FORGET),
        ("off", DeleteMode.FORGET),
        ("true", DeleteMode.PURGE),
        ("yes", DeleteMode.PURGE),
        ("1", DeleteMode.PURGE),
    ],
)
def test_delete_hard_is_parsed_boolean_not_truthy(hard, expected) -> None:
    """``hard`` 必须走 ``_bool_value``：字符串 "false" 不得触发不可恢复的 PURGE。"""
    api = _RecordingDeleteApi()

    status, body = _dispatch(api, "delete", {**_TARGET, "item_id": "u1", "hard": hard})

    assert status == 200, body
    assert api.selectors[-1].mode == expected, f"hard={hard!r}"


class _RecordingSearchApi:
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
        del query, context, security, filters, as_of, disclosure
        self.calls.append({"top_k": top_k, "with_trajectory": with_trajectory})
        return SimpleNamespace(items=[], trajectory=[])


@pytest.mark.parametrize(
    ("trace", "expected"),
    [("false", False), ("0", False), ("off", False), (False, False), ("true", True), (True, True)],
)
def test_search_trace_is_parsed_boolean_not_truthy(trace, expected) -> None:
    """``trace`` 必须走 ``_bool_value``：字符串 "false" 不得返回轨迹。"""
    api = _RecordingSearchApi()

    status, body = _dispatch(api, "search", {**_TARGET, "query": "q", "trace": trace})

    assert status == 200, body
    assert api.calls[-1]["with_trajectory"] is expected
    assert ("trajectory" in body) is expected


def test_search_rejects_unrecognized_boolean() -> None:
    """未识别的布尔串必须报错，不能静默当 False。"""
    api = _RecordingSearchApi()

    status, body = _dispatch(api, "search", {**_TARGET, "query": "q", "trace": "maybe"})

    assert status == 400
    assert body["error"] == "ValidationError"
    assert api.calls == []


# --- #226 枚举非法值 --------------------------------------------------------- #


def test_add_rejects_null_and_invalid_source() -> None:
    """``source: null`` / 非法字符串应在边界返回 400，而不是下沉成 500。"""
    for value in (None, "invalid", 3):
        api = SimpleNamespace(add=lambda *a, **k: pytest.fail("API must not be called"))

        status, body = _dispatch(api, "add", {**_TARGET, "content": "x", "source": value})

        assert status == 400, f"source={value!r}"
        assert body["error"] == "ValidationError"


def test_add_defaults_source_to_text() -> None:
    seen: list = []

    def add(content, scope, source, **kwargs):
        del content, scope, kwargs
        seen.append(source)
        return []

    status, body = _dispatch(SimpleNamespace(add=add), "add", {**_TARGET, "content": "x"})

    assert status == 200, body
    assert seen == [Modality.TEXT]


class _RecordingEvolveApi:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def evolve(
        self, scope, mode, channel=None, *, candidate=None, dreaming=None, interval=0, security=None
    ):
        del scope, candidate, dreaming, interval, security
        self.calls.append({"mode": mode, "channel": channel})
        return "job-1"


def test_evolve_rejects_null_and_invalid_mode() -> None:
    """``mode: null`` / 非法字符串应在边界返回 400，而不是下沉成 500。"""
    for value in (None, "nope"):
        api = _RecordingEvolveApi()

        status, body = _dispatch(api, "evolve", {**_TARGET, "mode": value})

        assert status == 400, f"mode={value!r}"
        assert body["error"] == "ValidationError"
        assert api.calls == []


def test_evolve_defaults_mode_to_extract() -> None:
    api = _RecordingEvolveApi()

    status, body = _dispatch(api, "evolve", _TARGET)

    assert status == 200, body
    assert api.calls[0]["mode"] is EvolveMode.EXTRACT


# --- 整数与布尔解析 ---------------------------------------------------------- #


@pytest.mark.parametrize("value", [True, False, 1.5, "abc", 0, -3])
def test_search_rejects_non_positive_or_non_integer_top_k(value) -> None:
    api = _RecordingSearchApi()

    status, body = _dispatch(api, "search", {**_TARGET, "query": "q", "k": value})

    assert status == 400, f"k={value!r}"
    assert body["error"] == "ValidationError"
    assert api.calls == []


def test_search_forwards_integer_top_k() -> None:
    api = _RecordingSearchApi()

    status, body = _dispatch(api, "search", {**_TARGET, "query": "q", "k": 7})

    assert status == 200, body
    assert api.calls[-1]["top_k"] == 7


class _RecordingSpaceApi:
    def __init__(self) -> None:
        self.specs: list = []
        self.patches: list = []
        self.members: list = []
        self.exports: list = []

    def create_space(self, spec, *, security):
        del security
        self.specs.append(spec)
        return SpaceInfo(
            org=spec.org,
            space=spec.space,
            display_name=spec.display_name,
            principal_path=spec.principal_path,
            policy=spec.policy,
            metadata=spec.metadata,
            owners=[] if spec.owner is None else [spec.owner],
        )

    def update_space(self, org, space, patch, *, security):
        del security
        self.patches.append(patch)
        return SpaceInfo(org=org, space=space)

    def add_space_member(self, org, space, member, *, security):
        del org, space, security
        self.members.append(member)

    def list_space_members(self, org, space, *, security):
        del org, space, security
        return self.members

    @staticmethod
    def list_spaces(org, *, security, status=None, limit=100, cursor=None):
        del org, security, status, limit, cursor
        return []

    def export_space(self, org, space, *, security, include_audit=True):
        del org, space, security
        self.exports.append(include_audit)
        return "export-1"


@pytest.mark.parametrize("value", [True, 1.5])
def test_list_spaces_rejects_non_integer_limit(value) -> None:
    api = _RecordingSpaceApi()

    status, body = _dispatch(api, "list_spaces", {"tenant_id": "acme", "limit": value})

    assert status == 400, f"limit={value!r}"
    assert body["error"] == "ValidationError"


@pytest.mark.parametrize(
    ("include_audit", "expected"), [("false", False), ("0", False), (False, False), ("true", True)]
)
def test_export_space_parses_include_audit_boolean(include_audit, expected) -> None:
    api = _RecordingSpaceApi()

    status, body = _dispatch(
        api,
        "export_space",
        {"tenant_id": "acme", "space": "team", "include_audit": include_audit},
    )

    assert status == 200, body
    assert api.exports == [expected]


def test_export_space_rejects_unrecognized_boolean() -> None:
    api = _RecordingSpaceApi()

    status, body = _dispatch(
        api, "export_space", {"tenant_id": "acme", "space": "team", "include_audit": "maybe"}
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert api.exports == []


# --- space owner / 成员 / 空容器 patch --------------------------------------- #


def test_create_space_forwards_owner() -> None:
    """``owner`` 是调用方填写的一等字段，漏传会让归属登记静默失效。"""
    api = _RecordingSpaceApi()

    status, body = _dispatch(
        api,
        "create_space",
        {"tenant_id": "acme", "space": "team", "owner": {"org": "acme", "user": "owner"}},
    )

    assert status == 200, body
    assert api.specs[0].owner == Scope(org="acme", user="owner")
    assert body["space"]["owners"] == [
        {"org": "acme", "space": "", "user": "owner", "agent": "", "session": ""}
    ]


def test_add_space_member_forwards_roles_and_timestamps() -> None:
    """成员两轴角色与时间是判定的实际依据，漏传会把成员永远建成最低档。"""
    api = _RecordingSpaceApi()
    expires_at = "2027-01-01T00:00:00+00:00"

    status, body = _dispatch(
        api,
        "add_space_member",
        {
            "tenant_id": "acme",
            "space": "team",
            "scope": "administrator",
            "member": {
                "scope": {"org": "acme", "user": "bob"},
                "content_role": "viewer",
                "governance_role": "manager",
                "expires_at": expires_at,
            },
        },
    )

    assert status == 200, body
    member = api.members[0]
    assert member.scope.user == "bob"
    assert member.content_role is SpaceContentRole.VIEWER
    assert member.governance_role is SpaceGovernanceRole.MANAGER
    assert member.expires_at == datetime(2027, 1, 1, tzinfo=UTC)

    status, body = _dispatch(
        api, "list_space_members", {"tenant_id": "acme", "space": "team"}
    )
    assert status == 200, body
    assert body["members"][0]["content_role"] == "viewer"
    assert body["members"][0]["governance_role"] == "manager"


def test_add_space_member_rejects_invalid_roles() -> None:
    for field, value in (("content_role", "boss"), ("governance_role", "boss")):
        api = _RecordingSpaceApi()

        status, body = _dispatch(
            api,
            "add_space_member",
            {
                "tenant_id": "acme",
                "space": "team",
                "scope": "administrator",
                "member": {"scope": {"org": "acme", "user": "bob"}, field: value},
            },
        )

        assert status == 400, f"{field}={value!r}"
        assert body["error"] == "ValidationError"
        assert api.members == []


def test_update_space_keeps_empty_policy_and_metadata_patch() -> None:
    """``policy={}`` / ``metadata={}`` 是「清空」而不是「未提供」，不得被丢弃。"""
    api = _RecordingSpaceApi()

    status, body = _dispatch(
        api,
        "update_space",
        {"tenant_id": "acme", "space": "team", "policy": {}, "metadata": {}},
    )

    assert status == 200, body
    assert api.patches[0].policy is not None
    assert api.patches[0].metadata == {}


def test_list_spaces_rejects_blank_status() -> None:
    """空串不是合法枚举，必须报错而不是被真值判断静默忽略。"""
    api = _RecordingSpaceApi()

    status, body = _dispatch(api, "list_spaces", {"tenant_id": "acme", "status": ""})

    assert status == 400
    assert body["error"] == "ValidationError"


# --- delete 选择器 ----------------------------------------------------------- #


def test_delete_forwards_tags_and_before() -> None:
    api = _RecordingDeleteApi()

    status, body = _dispatch(
        api,
        "delete",
        {**_TARGET, "tags": ["a", "b"], "before": "2024-01-01T00:00:00+00:00"},
    )

    assert status == 200, body
    selector = api.selectors[-1]
    assert selector.unit_ids == []
    assert selector.tags == ["a", "b"]
    assert selector.before == datetime(2024, 1, 1, tzinfo=UTC)
    assert body["item_id"] is None


def test_delete_forwards_filters_dsl() -> None:
    api = _RecordingDeleteApi()

    status, body = _dispatch(api, "delete", {**_TARGET, "filters": {"topic": {"eq": "x"}}})

    assert status == 200, body
    assert api.selectors[-1].filters is not None


def test_delete_requires_at_least_one_criterion() -> None:
    api = _RecordingDeleteApi()

    status, body = _dispatch(api, "delete", dict(_TARGET))

    assert status == 400
    assert body["error"] == "ValidationError"
    assert api.selectors == []


# --- update mode ------------------------------------------------------------- #


def test_update_rejects_blank_mode() -> None:
    api = SimpleNamespace(update=lambda *a, **k: pytest.fail("API must not be called"))

    status, body = _dispatch(api, "update", {**_TARGET, "item_id": "u1", "mode": ""})

    assert status == 400
    assert body["error"] == "ValidationError"


# --- 视频摄入 --------------------------------------------------------------- #


def test_video_add_forwards_occurred_at_and_rejects_bad_assets() -> None:
    calls: list[dict] = []

    def submit_ingest(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(job=SimpleNamespace(id="ing-1", status="pending"), reused=False)

    config = {
        "memory_api": {
            "normalizer": {"default": {"params": {"routes": {"video": {}}}}},
            "evolver": {"video": {}},
        }
    }
    srv = SimpleNamespace(
        api=SimpleNamespace(submit_ingest=submit_ingest),
        config=SimpleNamespace(settings=config),
    )
    occurred_at = "2025-03-04T05:06:07+00:00"
    base = {
        **_TARGET,
        "source": "video",
        "uri": "file:///video.mp4",
        "occurred_at": occurred_at,
    }

    status, body = handler.dispatch(srv, build_legacy_dispatch_request("add", base))

    assert status == 200, body
    assert calls[0]["occurred_at"] == datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)

    status, body = handler.dispatch(
        srv, build_legacy_dispatch_request("add", {**base, "assets": {"bad": "shape"}})
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert len(calls) == 1


# --- grant / revoke --------------------------------------------------------- #


def test_grant_forwards_actions_and_expiry() -> None:
    """``actions`` / ``expires_at`` 写死会让「授写权限」「授有限期」静默落成永久只读。"""
    seen: list = []
    api = SimpleNamespace(grant=lambda grant, *, security: seen.append(grant))

    status, body = _dispatch(
        api,
        "grant",
        {
            **_TARGET,
            "grantee": {"org": "acme", "user": "reader"},
            "actions": ["read", "write"],
            "expires_at": "2026-10-01T00:00:00+00:00",
        },
    )

    assert status == 200, body
    assert seen[0].actions == frozenset({Action.READ, Action.WRITE})
    assert seen[0].expires_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_grant_defaults_to_read_and_rejects_unknown_action() -> None:
    seen: list = []
    api = SimpleNamespace(grant=lambda grant, *, security: seen.append(grant))

    status, body = _dispatch(
        api, "grant", {**_TARGET, "grantee": {"org": "acme", "user": "reader"}}
    )
    assert status == 200, body
    assert seen[0].actions == frozenset({Action.READ})

    api = SimpleNamespace(grant=lambda grant, *, security: seen.append(grant))
    status, body = _dispatch(
        api,
        "grant",
        {**_TARGET, "grantee": {"org": "acme", "user": "reader"}, "actions": ["fly"]},
    )
    assert status == 400
    assert body["error"] == "ValidationError"


def test_revoke_forwards_actions_and_expiry() -> None:
    seen: list = []
    api = SimpleNamespace(revoke=lambda grant, *, security: seen.append(grant))

    status, body = _dispatch(
        api,
        "revoke",
        {
            **_TARGET,
            "grantee": {"org": "acme", "user": "reader"},
            "actions": "write",
        },
    )

    assert status == 200, body
    assert seen[0].actions == frozenset({Action.WRITE})
