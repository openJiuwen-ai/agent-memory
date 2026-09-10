# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""结构条件的类型校验、索引字段身份与 MemoryUnit 真源复核。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HIERARCHY_INDEX_KEYS,
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    MemoryUnit,
    ParsedQuery,
    hierarchy_index_metadata,
    is_retrieval_candidate,
    matches_memory_unit,
    normalize,
)
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery, matches_hierarchy

pytestmark = pytest.mark.unit
MOMENT = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("field_name", ["id", "unit_id", "user_metadata.id"])
def test_unit_id_filter_alias_preserves_index_and_namespace(field_name: str) -> None:
    expected = "user_metadata.id" if field_name.startswith("user_metadata.") else "unit_id"
    expression = normalize(FilterClause(field_name, FilterOp.EQ, "leaf"))
    assert expression == FilterClause(expected, FilterOp.EQ, "leaf")
    assert normalize(expression) == expression


@pytest.fixture(name="node")
def hierarchy_node_fixture():
    return MemoryUnit(id="leaf", hierarchy=HierarchyRef(
        kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT, parent_id="parent",
        span_start=MOMENT, span_end=MOMENT + timedelta(minutes=1),
    ))


@pytest.mark.parametrize("values", [
    {"hierarchy_kind": "time"},
    {"hierarchy_role": HierarchyRole.SNAPSHOT},
    {"span_start": MOMENT, "span_end": MOMENT},
    {"hierarchy_kind": HierarchyKind.TIME, "hierarchy_role": "snapshot"},
    {"hierarchy_kind": HierarchyKind.TIME, "span_start": MOMENT},
    {"hierarchy_kind": HierarchyKind.TIME, "span_end": MOMENT},
    {"hierarchy_kind": HierarchyKind.TIME, "span_start": "now", "span_end": "later"},
    {"hierarchy_kind": HierarchyKind.TIME, "span_start": MOMENT,
     "span_end": MOMENT - timedelta(microseconds=1)},
])
def test_hierarchy_query_rejects_invalid_explicit_fields(values) -> None:
    with pytest.raises(ValidationError):
        HierarchyQuery(**values)


def test_query_object_normalizes_utc_without_mutating_source() -> None:
    naive = MOMENT.replace(tzinfo=None)
    source = ParsedQuery(hierarchy_kind=HierarchyKind.TIME, span_start=naive, span_end=MOMENT)
    typed = HierarchyQuery.from_query(source)
    assert typed.enabled
    assert typed.span_start == typed.span_end == MOMENT
    assert source.span_start.tzinfo is None
    assert not HierarchyQuery().enabled


@pytest.mark.parametrize("field_name", sorted(HIERARCHY_INDEX_KEYS))
def test_structural_filter_names_survive_repeated_normalization(field_name) -> None:
    original = FilterClause(field_name, FilterOp.EQ, "sample")
    assert normalize(normalize(original)).field == field_name
    user_filter = FilterClause(f"user_metadata.{field_name}", FilterOp.EQ, "sample")
    assert normalize(user_filter).field == f"user_metadata.{field_name}"


def test_kernel_projection_and_metadata_namespaces_never_fallback(node) -> None:
    projection = hierarchy_index_metadata(node.hierarchy)
    node.user_metadata = {name: "user-copy" for name in projection}
    node.system_metadata = {name: "stale-index-copy" for name in projection}
    for field_name, actual in projection.items():
        assert matches_memory_unit(node, normalize(FilterClause(field_name, FilterOp.EQ, actual)))
        assert not matches_memory_unit(node, FilterClause(field_name, FilterOp.EQ, "user-copy"))
        assert matches_memory_unit(
            node, FilterClause(f"user_metadata.{field_name}", FilterOp.EQ, "user-copy"),
        )
        assert matches_memory_unit(
            node, FilterClause(f"system_metadata.{field_name}", FilterOp.EQ, "stale-index-copy"),
        )


@pytest.mark.parametrize("delta, expected", [(0, True), (1, False), (-1, True)])
def test_structural_window_is_closed_and_keeps_microsecond_precision(node, delta, expected) -> None:
    query_start = node.hierarchy.span_end + timedelta(microseconds=delta)
    query = HierarchyQuery(
        hierarchy_kind=HierarchyKind.TIME, span_start=query_start, span_end=query_start,
    )
    assert matches_hierarchy(node, query) is expected


@pytest.mark.parametrize("field_name, value", [
    ("kind", HierarchyKind.TOPIC),
    ("role", HierarchyRole.TIME_SPAN),
    ("status", HierarchyStatus.DISMISSED),
    ("span_start", None),
    ("span_end", MOMENT - timedelta(seconds=1)),
])
def test_explicit_structure_query_rechecks_authoritative_fields(node, field_name, value) -> None:
    node.system_metadata.update(hierarchy_index_metadata(node.hierarchy))
    setattr(node.hierarchy, field_name, value)
    query = HierarchyQuery(hierarchy_kind=HierarchyKind.TIME, hierarchy_role=HierarchyRole.SNAPSHOT)
    assert not matches_hierarchy(node, query)


def test_time_nodes_without_spans_are_ineligible_even_for_unbounded_query(node) -> None:
    node.hierarchy.span_start = node.hierarchy.span_end = None
    assert not matches_hierarchy(node, HierarchyQuery(hierarchy_kind=HierarchyKind.TIME))
    node.hierarchy.kind = HierarchyKind.TOPIC
    node.hierarchy.role = HierarchyRole.NODE
    assert matches_hierarchy(node, HierarchyQuery(hierarchy_kind=HierarchyKind.TOPIC))


def test_ordinary_query_does_not_require_or_filter_hierarchy(node) -> None:
    node.hierarchy.status = HierarchyStatus.DISMISSED
    assert matches_hierarchy(node, HierarchyQuery())
    assert matches_hierarchy(MemoryUnit(id="ordinary"), HierarchyQuery())
    assert not matches_hierarchy(MemoryUnit(id="ordinary"), HierarchyQuery(
        hierarchy_kind=HierarchyKind.TIME,
    ))


def test_aggregate_candidate_query_retains_lifecycle_and_user_filters(node) -> None:
    query = ParsedQuery(hierarchy_kind=HierarchyKind.TIME)
    node.user_metadata["project"] = "alpha"
    filters = FilterClause("user_metadata.project", FilterOp.EQ, "alpha")
    assert is_retrieval_candidate(node, query, filters=filters)
    assert not is_retrieval_candidate(node, query, filters=replace(filters, value="beta"))
    node.lifecycle = LifecycleState.ARCHIVED
    assert not is_retrieval_candidate(node, query, filters=filters)
    assert is_retrieval_candidate(node, replace(query, include_archived=True), filters=filters)
    node.hierarchy.status = HierarchyStatus.DISMISSED
    assert not is_retrieval_candidate(node, replace(query, include_archived=True), filters=filters)
