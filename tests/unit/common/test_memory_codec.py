"""memory_codec: round-trip consistency, versioning, and tolerant evolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.memory import LifecycleState, MemoryTier, Modality
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.scope import Scope

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
    unit.metadata = {"confidence": "0.9"}

    back = loads(dumps(unit))

    assert back.id == "u1"
    assert back.content == "alice likes coffee"
    assert back.scope == unit.scope
    assert back.tier == MemoryTier.SEMANTIC
    assert back.lifecycle == LifecycleState.ACTIVE
    assert back.supersedes == "u0"
    assert back.tags == ["x", "y"]
    assert back.metadata == {"confidence": "0.9"}
    assert back.temporal.t_valid == t_valid


def test_dumps_carries_schema_version(unit_factory) -> None:
    obj = json.loads(dumps(unit_factory("u1", "x")).decode("utf-8"))
    assert obj["_v"] == 3


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


def test_loads_v2_scope_defaults_space() -> None:
    raw = json.dumps(
        {
            "_v": 2,
            "id": "old2",
            "scope": ["o", "u", "a", "s"],
            "tier": "semantic",
            "segments": [{"content": "旧内容", "assets": [], "source": "text"}],
        }
    ).encode("utf-8")

    back = loads(raw)

    assert back.scope.org == "o"
    assert back.scope.space == ""
    assert back.scope.user == "u"
    assert back.scope.agent == "a"
    assert back.scope.session == "s"


def test_loads_v1_flat_data_becomes_single_segment() -> None:
    raw = json.dumps(
        {
            "_v": 1,
            "id": "old1",
            "scope": ["o", "a", "", ""],
            "tier": "semantic",
            "content": "旧内容",
            "assets": ["a.png"],
            "source": "image",
        }
    ).encode("utf-8")

    back = loads(raw)

    assert len(back.segments) == 1
    assert back.content == "旧内容"
    assert back.assets == ["a.png"]
    assert back.source == Modality.IMAGE


def test_loads_ignores_unknown_fields(unit_factory) -> None:
    obj = json.loads(dumps(unit_factory("u1", "x")).decode("utf-8"))
    obj["_future_field"] = {"nested": 1}

    back = loads(json.dumps(obj).encode("utf-8"))

    assert back.id == "u1"


def test_loads_takes_defaults_for_missing_fields() -> None:
    raw = json.dumps({"id": "only_id"}).encode("utf-8")

    back = loads(raw)

    assert back.id == "only_id"
    assert back.content == ""
    assert back.tier == MemoryTier.EPISODIC
    assert back.source == Modality.TEXT
    assert back.lifecycle == LifecycleState.ACTIVE
    assert back.scope.org == ""
    assert back.temporal.t_valid is None
