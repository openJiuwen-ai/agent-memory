"""memory_codec: round-trip consistency, versioning, and tolerant evolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Segment
from jiuwen_memory.common.type_def.memory import ChunkVector, LifecycleState, MemoryTier, Modality
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
