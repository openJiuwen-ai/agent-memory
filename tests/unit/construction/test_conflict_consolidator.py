"""ConflictConsolidator 的单元测试（聚类 / 规划 / 解析 / 幂等标记）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.evolver_impl.conflict_consolidator import (
    ConflictConsolidator,
)
from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
from tests.unit.construction.fixtures import MemoryKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="org", user="user")


class _FakeLLM:
    """按 prompt 内容分派：关系检测 prompt → relation 响应；动作规划 prompt → action 响应。"""

    def __init__(
        self,
        *,
        relation: object = None,
        action: object = None,
        calls: int | None = None,
    ) -> None:
        self._relation = relation if relation is not None else {"issue_groups": []}
        self._action = action if action is not None else {"actions": []}
        self.calls = 0

    def chat(self, messages, **_options) -> str:
        self.calls += 1
        content = messages[0].content if messages else ""
        if "conflict detector" in content:
            return self._encode(self._relation)
        return self._encode(self._action)

    @staticmethod
    def _encode(value: object) -> str:
        return value if isinstance(value, str) else json.dumps(value)


def _property(
    unit_id: str,
    entity_id: str,
    property_name: str,
    content: str,
    *,
    name: str = "Jon",
    entity_type: str = "person",
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=content)],
        temporal=Temporal(t_valid=datetime(2023, 1, 1, tzinfo=UTC)),
        system_metadata={
            "extraction_mode": "schema",
            "schema_entity_id": entity_id,
            "schema_entity_name": name,
            "schema_entity_type": entity_type,
            "schema_property_name": property_name,
            "schema_property_operation": "set",
        },
        lifecycle=lifecycle,
    )


def _seed(kv: MemoryKVStore, units: list[MemoryUnit], *, index: bool = True) -> None:
    for unit in units:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    if index:
        SchemaPropertyIndex(kv).upsert(units)


def _consolidator(kv: MemoryKVStore, llm: _FakeLLM | None = None) -> ConflictConsolidator:
    return ConflictConsolidator(kv=kv, llm=llm or _FakeLLM())


# ---------------------------------------------------------------------------
# P1 聚类
# ---------------------------------------------------------------------------


def test_cluster_groups_by_entity_and_property_without_overlap() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon started his own business"),
        _property("m3", "E-jon", "hobby", "Jon plays guitar"),
        _property("m4", "E-gina", "occupation", "Gina is a nurse"),
    ]
    _seed(kv, units)

    clusters = _consolidator(kv).cluster(units)

    keys = {(c.entity_id, c.property_name) for c in clusters}
    assert keys == {
        ("E-jon", "occupation"),
        ("E-jon", "hobby"),
        ("E-gina", "occupation"),
    }
    # 无重叠：每条记忆只落一簇
    seen: list[str] = [u.id for c in clusters for u in c.memories]
    assert sorted(seen) == ["m1", "m2", "m3", "m4"]
    assert len(seen) == len(set(seen))


def test_cluster_only_expands_seed_keys() -> None:
    """seed 只触及 (Jon, occupation) → 只返回该键；同属性旧值仍拉入，hobby 不展开。"""
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon is a teacher"),
        _property("m3", "E-jon", "hobby", "Jon plays guitar"),
        _property("m4", "E-jon", "hobby", "Jon plays piano"),
    ]
    _seed(kv, units)

    # 只把 m2 当本轮新证据
    clusters = _consolidator(kv).cluster([units[1]])

    assert len(clusters) == 1
    assert clusters[0].property_name == "occupation"
    assert {u.id for u in clusters[0].memories} == {"m1", "m2"}


def test_cluster_falls_back_to_field_grouping_without_index() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon left banking"),
    ]
    _seed(kv, units, index=False)  # 不建反向索引 → 走退化全表扫

    clusters = _consolidator(kv).cluster(units)

    assert len(clusters) == 1
    assert clusters[0].entity_id == "E-jon"
    assert clusters[0].property_name == "occupation"
    assert {u.id for u in clusters[0].memories} == {"m1", "m2"}


def test_non_property_seeds_are_ignored() -> None:
    kv = MemoryKVStore()
    source = MemoryUnit(
        id="src",
        scope=_SCOPE,
        segments=[Segment(content="raw text")],
        system_metadata={"memory_role": "source_evidence"},
    )
    assert _consolidator(kv).cluster([source]) == []


# ---------------------------------------------------------------------------
# P2 噪声过滤 / P3 去重
# ---------------------------------------------------------------------------


def test_singleton_cluster_is_filtered() -> None:
    kv = MemoryKVStore()
    units = [_property("m1", "E-jon", "occupation", "Jon is a banker")]
    _seed(kv, units)

    plan = _consolidator(kv).plan(units)

    assert not plan.additions
    assert not plan.replacements
    assert not plan.retire


def test_exact_duplicate_is_retired() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon  Is  A BANKER"),  # 归一化后同内容
    ]
    _seed(kv, units)

    plan = _consolidator(kv).plan(units)

    retired_ids = {u.id for u in plan.retire}
    assert len(retired_ids) == 1  # 保留一条、退一条
    assert not plan.additions


# ---------------------------------------------------------------------------
# P5 动作 → 计划映射
# ---------------------------------------------------------------------------


def test_archive_action_retires_unit() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon runs his own company"),
    ]
    _seed(kv, units)
    llm = _FakeLLM(
        relation={
            "issue_groups": [
                {"issue_type": "conflict", "memory_ids": ["m1", "m2"], "confidence": "high"}
            ]
        },
        action={"actions": [{"type": "archive", "unit_id": "m1", "reason": "superseded"}]},
    )

    plan = _consolidator(kv, llm).plan(units)

    assert {u.id for u in plan.retire} == {"m1"}
    assert not plan.additions


def test_merge_action_adds_consolidated_unit_and_retires_sources() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon became a banker in 2022"),
        _property("m2", "E-jon", "occupation", "In 2023 Jon left banking"),
    ]
    _seed(kv, units)
    llm = _FakeLLM(
        relation={
            "issue_groups": [
                {
                    "issue_type": "complementary",
                    "memory_ids": ["m1", "m2"],
                    "confidence": "medium",
                }
            ]
        },
        action={
            "actions": [
                {
                    "type": "merge",
                    "source_unit_ids": ["m1", "m2"],
                    "target_content": "Jon was a banker (2022) and left banking in 2023",
                }
            ]
        },
    )

    plan = _consolidator(kv, llm).plan(units)

    assert len(plan.additions) == 1
    merged = plan.additions[0]
    assert merged.content == "Jon was a banker (2022) and left banking in 2023"
    assert set(merged.provenance) >= {"m1", "m2"}
    # 归属键继承自簇
    assert merged.system_metadata["schema_entity_id"] == "E-jon"
    assert merged.system_metadata["schema_property_name"] == "occupation"
    assert merged.lifecycle is LifecycleState.ACTIVE
    assert {u.id for u in plan.retire} == {"m1", "m2"}


def test_make_property_unit_clears_derived_fields() -> None:
    """内容改写后，旧单元的 L0/L1 摘要与向量不得被继承。"""
    from jiuwen_memory.common.type_def import ChunkVector
    from jiuwen_memory.construction.evolver_impl.conflict_consolidator import (
        Cluster,
        _make_property_unit,
    )

    old = _property("m1", "E-jon", "occupation", "Jon is a banker")
    old.layers.l0 = "旧摘要"
    old.layers.l1 = "旧概览"
    old.vectors = [ChunkVector(id="c1", seq=0, vector=[0.1, 0.2])]
    cluster = Cluster(entity_id="E-jon", property_name="occupation", memories=[old])

    unit = _make_property_unit(cluster, "Jon is a teacher", ["m1"])

    assert unit.content == "Jon is a teacher"
    assert unit.layers.l0 == ""
    assert unit.layers.l1 == ""
    assert unit.vectors == []


def test_update_action_replaces_target_and_retires_old() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon is a senior banker"),
    ]
    _seed(kv, units)
    llm = _FakeLLM(
        relation={
            "issue_groups": [
                {"issue_type": "conflict", "memory_ids": ["m1", "m2"], "confidence": "high"}
            ]
        },
        action={
            "actions": [
                {"type": "update", "unit_id": "m1", "content": "Jon is a senior banker in 2023"}
            ]
        },
    )

    plan = _consolidator(kv, llm).plan(units)

    assert len(plan.replacements) == 1
    assert plan.replacements[0].supersedes == "m1"
    assert {u.id for u in plan.retire} == {"m1"}


# ---------------------------------------------------------------------------
# 解析健壮性
# ---------------------------------------------------------------------------


def test_llm_parse_failure_is_isolated() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon left banking"),
    ]
    _seed(kv, units)
    llm = _FakeLLM(relation="not json at all", action="also not json")

    plan = _consolidator(kv, llm).plan(units)

    assert not plan.additions
    assert not plan.replacements
    assert not plan.retire


def test_issue_group_with_unknown_ids_is_ignored() -> None:
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon left banking"),
    ]
    _seed(kv, units)
    # 引用了不在簇内的 id，且有效 id 不足 2 条 → 该组被丢弃，不下发动作规划
    llm = _FakeLLM(
        relation={
            "issue_groups": [
                {"issue_type": "conflict", "memory_ids": ["m1", "ghost"], "confidence": "high"}
            ]
        },
        action={"actions": [{"type": "archive", "unit_id": "m1"}]},
    )

    plan = _consolidator(kv, llm).plan(units)

    assert not plan.retire
    assert llm.calls == 1  # 关系检测被调用，动作规划未触发


# ---------------------------------------------------------------------------
# D7：已 settled 的簇跳过（防重复巩固）
# ---------------------------------------------------------------------------


def test_settled_cluster_is_skipped() -> None:
    """簇内全部记忆都带 consolidation_done_at → 不调 LLM、不产出计划。"""
    kv = MemoryKVStore()
    units = [
        _property("m1", "E-jon", "occupation", "Jon is a banker"),
        _property("m2", "E-jon", "occupation", "Jon left banking"),
    ]
    for unit in units:
        unit.system_metadata["consolidation_done_at"] = "2026-01-01T00:00:00+00:00"
    _seed(kv, units)
    llm = _FakeLLM()

    plan = _consolidator(kv, llm).plan(units)

    assert not plan.additions
    assert not plan.replacements
    assert not plan.retire
    assert llm.calls == 0  # settled → 完全跳过 LLM


def test_cluster_with_unmarked_memory_is_processed() -> None:
    """簇内只要有一条未标记（新证据）→ 不 settled，正常处理。"""
    kv = MemoryKVStore()
    marked = _property("m1", "E-jon", "occupation", "Jon is a banker")
    marked.system_metadata["consolidation_done_at"] = "2026-01-01T00:00:00+00:00"
    fresh = _property("m2", "E-jon", "occupation", "Jon left banking")
    _seed(kv, [marked, fresh])
    llm = _FakeLLM(relation={"issue_groups": []})

    _consolidator(kv, llm).plan([marked, fresh])

    assert llm.calls >= 1  # 关系检测被调用


# ---------------------------------------------------------------------------
# S1：有效时间优先事实时间（t_event），而非抽取/写入时刻
# ---------------------------------------------------------------------------


def test_effective_time_prefers_event_time() -> None:
    from jiuwen_memory.construction.evolver_impl.conflict_consolidator import _effective_time

    unit = _property("m1", "E-jon", "occupation", "x")
    unit.temporal.t_event = datetime(2020, 1, 1, tzinfo=UTC)
    unit.temporal.t_ingest = datetime(2024, 1, 1, tzinfo=UTC)
    assert _effective_time(unit) == datetime(2020, 1, 1, tzinfo=UTC)


def test_effective_time_falls_back_to_ingest_without_event() -> None:
    from jiuwen_memory.construction.evolver_impl.conflict_consolidator import _effective_time

    unit = _property("m1", "E-jon", "occupation", "x")
    unit.temporal.t_event = None
    unit.temporal.t_ingest = datetime(2024, 1, 1, tzinfo=UTC)
    assert _effective_time(unit) == datetime(2024, 1, 1, tzinfo=UTC)


def test_plan_tolerates_mixed_timezone_awareness() -> None:
    """同内容的两条记忆，一条 aware、一条 naive，去重排序时不得抛 TypeError。"""
    kv = MemoryKVStore()
    aware = _property("m1", "E-jon", "occupation", "Jon is a banker")
    aware.temporal.t_event = datetime(2024, 1, 1, tzinfo=UTC)
    naive = _property("m2", "E-jon", "occupation", "Jon is a banker")
    naive.temporal.t_event = datetime(2023, 1, 1)  # naive：模拟未经归一化解析
    _seed(kv, [aware, naive])

    plan = _consolidator(kv).plan([aware, naive])

    assert [unit.id for unit in plan.retire] == ["m2"]
