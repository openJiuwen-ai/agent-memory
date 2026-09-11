"""memory_codec: round-trip consistency, versioning, and tolerant evolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    MemoryUnit,
    Segment,
)
from jiuwen_memory.common.type_def.memory import ChunkVector, LifecycleState, MemoryTier, Modality
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.scope import Scope
from tests.unit.common.fixtures import hierarchy_payload, with_hierarchy

pytestmark = pytest.mark.unit


def test_scope_space_is_keyword_only_and_old_positional_order_is_preserved() -> None:
    scope = Scope("org", "user", "agent", "session")

    assert scope == Scope(
        org="org",
        space="",
        user="user",
        agent="agent",
        session="session",
    )


def test_roundtrip_preserves_fields(unit_factory) -> None:
    t_valid = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    unit = unit_factory(
        "u1",
        "alice likes coffee",
        t_valid=t_valid,
        supersedes="u0",
        tags=["x", "y"],
    )
    unit.system_metadata = {"confidence": "0.9"}
    unit.user_metadata = {"project": "alpha"}

    back = loads(dumps(unit))

    assert back.id == "u1"
    assert back.content == "alice likes coffee"
    assert back.scope == unit.scope
    assert back.tier == MemoryTier.SEMANTIC
    assert back.lifecycle == LifecycleState.ACTIVE
    assert back.supersedes == "u0"
    assert back.tags == ["x", "y"]
    assert back.system_metadata == {"confidence": "0.9"}
    assert back.user_metadata == {"project": "alpha"}
    assert back.temporal.t_valid == t_valid


def test_dumps_carries_schema_version(unit_factory) -> None:
    obj = json.loads(dumps(unit_factory("u1", "x")).decode("utf-8"))
    assert obj["_v"] == 4


def test_roundtrip_preserves_multiple_segments() -> None:
    unit = MemoryUnit(
        id="u1",
        scope=Scope(org="o", space="p", user="a"),
        segments=[
            Segment(content="文本段", assets=["img1"], source=Modality.TEXT),
            Segment(content="图描述", assets=["img2"], source=Modality.IMAGE),
        ],
    )

    back = loads(dumps(unit))

    assert len(back.segments) == 2
    assert back.segments[1].content == "图描述"
    assert back.segments[1].source == Modality.IMAGE
    assert back.content == "文本段\n图描述"  # 折叠视图：换行连接
    assert back.assets == ["img1", "img2"]  # 折叠视图：扁平合并
    assert back.source == Modality.TEXT  # 折叠视图：主模态=首段
    assert back.scope.space == "p"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_loads_rejects_pre_split_metadata_versions(version: int) -> None:
    raw = json.dumps(
        {
            "_v": version,
            "id": "old2",
            "scope": ["o", "u", "a", "s"],
            "tier": "semantic",
            "segments": [{"content": "旧内容", "assets": [], "source": "text"}],
        }
    ).encode("utf-8")

    with pytest.raises(ValueError, match="explicit metadata migration"):
        loads(raw)


def test_loads_ignores_unknown_fields(unit_factory) -> None:
    obj = json.loads(dumps(unit_factory("u1", "x")).decode("utf-8"))
    obj["_future_field"] = {"nested": 1}

    back = loads(json.dumps(obj).encode("utf-8"))

    assert back.id == "u1"


def test_roundtrip_preserves_vector(unit_factory) -> None:
    """vectors 字段（F08 加字段兼容演进）：chunk 级向量往返保留，缺省读为空列表。"""
    unit = unit_factory("u1", "alice likes coffee")
    unit.vectors = [ChunkVector(id="0", seq=0, vector=[0.1, -0.2, 0.3])]

    back = loads(dumps(unit))

    assert back.vectors == [ChunkVector(id="0", seq=0, vector=[0.1, -0.2, 0.3])]

    # 老数据无 vectors 键：缺省取空列表，无迁移读出
    obj = json.loads(dumps(unit_factory("u2", "x")).decode("utf-8"))
    obj.pop("vectors")
    assert loads(json.dumps(obj).encode("utf-8")).vectors == []


def test_loads_rejects_unversioned_legacy_payload() -> None:
    raw = json.dumps({"id": "only_id"}).encode("utf-8")

    with pytest.raises(ValueError, match="explicit metadata migration"):
        loads(raw)


# -- hierarchy（F08 树结构）：加字段属兼容演进，不升 _v --------------------- #


def test_hierarchy_roundtrip_preserves_all_fields(unit_factory) -> None:
    child_scope = Scope(org="acme", space="prod", user="z", agent="a1", session="s_am")
    home_scope = Scope(org="acme", space="prod", user="z")
    span_start = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
    span_end = datetime(2026, 8, 18, 11, 0, tzinfo=timezone.utc)
    unit = unit_factory("p1", "上午调 bug")
    unit.vectors = [ChunkVector(id="p1-0", seq=0, vector=[0.1, 0.2])]
    unit.hierarchy = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.TIME_SPAN,
        parent_id="scene-1",
        child_ids=["leaf-1"],
        child_scopes=[child_scope],
        parent_scope=home_scope,
        span_start=span_start,
        span_end=span_end,
        ordinal=3,
        status=HierarchyStatus.DISMISSED,
    )

    back = loads(dumps(unit))

    assert back.hierarchy.kind is HierarchyKind.TIME
    assert back.vectors == unit.vectors, "树结构和向量字段必须同时往返保留"
    assert back.hierarchy.role is HierarchyRole.TIME_SPAN
    assert back.hierarchy.parent_id == "scene-1"
    assert back.hierarchy.child_ids == ["leaf-1"]
    assert back.hierarchy.child_scopes == [child_scope]
    assert back.hierarchy.parent_scope == home_scope
    assert back.hierarchy.span_start == span_start
    assert back.hierarchy.span_end == span_end
    assert back.hierarchy.ordinal == 3
    assert back.hierarchy.status is HierarchyStatus.DISMISSED


def test_empty_hierarchy_is_not_written_out(unit_factory) -> None:
    unit = unit_factory("u1", "普通记忆")

    assert hierarchy_payload(unit) is None, "空结构不占字节，旧记录形状保持不变"
    assert loads(dumps(unit)).hierarchy.is_empty


def test_legacy_payload_without_hierarchy_reads_as_empty(unit_factory) -> None:
    payload = json.loads(dumps(unit_factory("u1", "老数据")).decode("utf-8"))
    payload.pop("hierarchy", None)

    back = loads(json.dumps(payload).encode("utf-8"))

    assert back.hierarchy.is_empty, "缺 hierarchy 的历史数据必须免迁移读出"


def test_non_object_hierarchy_degrades_to_empty(unit_factory) -> None:
    back = with_hierarchy(unit_factory("u1", "坏数据"), ["not", "an", "object"])

    assert back.hierarchy.is_empty


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "wormhole"), ("role", "not_a_role"), ("status", "unknown")],
)
def test_unknown_enum_degrades_whole_hierarchy_to_empty(
    unit_factory, field: str, value: str
) -> None:
    """未知枚举不得构造半有效结构——整段降级为空，避免坏数据参与建树。"""
    unit = unit_factory("u1", "未来版本数据")
    unit.hierarchy = HierarchyRef(
        kind=HierarchyKind.TIME,
        role=HierarchyRole.SNAPSHOT,
        span_start=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
        span_end=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
    )
    patch = json.loads(dumps(unit).decode("utf-8"))["hierarchy"]
    patch[field] = value

    back = with_hierarchy(unit, patch)

    assert back.hierarchy.is_empty


def test_unparsable_span_degrades_whole_hierarchy_to_empty(unit_factory) -> None:
    patch = {
        "kind": "time",
        "role": "snapshot",
        "parent_id": "",
        "child_ids": [],
        "child_scopes": [],
        "parent_scope": None,
        "span_start": "not-a-time",
        "span_end": "not-a-time",
        "ordinal": 0,
        "status": "active",
    }

    back = with_hierarchy(unit_factory("u1", "坏时间"), patch)

    assert back.hierarchy.is_empty


def test_unknown_hierarchy_fields_are_ignored(unit_factory) -> None:
    patch = {
        "kind": "topic",
        "role": "node",
        "parent_id": "",
        "child_ids": [],
        "child_scopes": [],
        "parent_scope": None,
        "span_start": None,
        "span_end": None,
        "ordinal": 0,
        "status": "active",
        "future_field": {"nested": 1},
    }

    back = with_hierarchy(unit_factory("u1", "未来字段"), patch)

    assert back.hierarchy.kind is HierarchyKind.TOPIC, "未知扩展字段应被忽略而非拒绝"
    assert back.hierarchy.role is HierarchyRole.NODE


@pytest.mark.parametrize(
    "patch",
    [
        {"parent_scope": ["other"]},
        {"parent_scope": ["acme", "prod", "u", "a", 3]},
        {"child_scopes": [None, ["acme", "prod", "u", "a", "s2"]]},
        {"child_scopes": [["acme", "prod", "u", "a", "s2"]]},
        {"child_scopes": "invalid"},
        {"child_ids": "c1"},
        {"child_ids": ["c1", None]},
    ],
)
def test_invalid_hierarchy_references_degrade_without_retargeting(unit_factory, patch) -> None:
    payload = {
        "kind": "topic",
        "role": "node",
        "child_ids": ["c1", "c2"],
        "child_scopes": [
            ["acme", "prod", "u", "a", "s1"],
            ["acme", "prod", "u", "a", "s2"],
        ],
    }
    payload.update(patch)

    back = with_hierarchy(unit_factory("p", "损坏的引用"), payload)

    assert back is not None, "结构降级不应丢弃记忆内容"
    assert back.hierarchy.is_empty, "非法位置不得被删除或重解释为 owner Scope"
    assert back.content == "损坏的引用", "结构降级不应改变记忆内容"


def test_hierarchy_roundtrip_preserves_metadata_and_strips_transient_keys(unit_factory) -> None:
    unit = unit_factory("leaf", "一条叶记忆")
    unit.hierarchy = HierarchyRef(kind=HierarchyKind.TOPIC, role=HierarchyRole.NODE)
    unit.system_metadata = {"pipeline": "chat", "coords": {"project": "p"}, "route_ctx": object()}
    unit.user_metadata = {"hierarchy_kind": "business-value", "priority": 3}

    back = loads(dumps(unit))

    assert back is not None, "有效记忆必须可读回"
    assert back.hierarchy == unit.hierarchy, "结构字段应独立保留"
    assert back.system_metadata == {"pipeline": "chat"}, "瞬态键仍不能进入持久化数据"
    assert back.user_metadata == unit.user_metadata, "用户同名键不得被结构序列化消费"
