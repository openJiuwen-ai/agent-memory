"""内存全文/向量 search 与向量 recall 必须在截断前执行同一过滤语义。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import FilterClause, FilterGroup, FilterLogic, FilterOp, Scope
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery, VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

pytestmark = pytest.mark.unit


def search_ids(backend: str, metadata: list[dict], expr, limit: int = 10) -> list[str]:
    """等分候选按插入顺序排列；观察过滤是否先于 top-k 生效。"""
    scope = Scope(org="org", user="alice")
    if backend == "fulltext":
        store = InMemoryFulltextStore(WhitespaceTokenizer())
        documents = [Document(str(index), "recall", item) for index, item in enumerate(metadata)]
        store.insert(scope, documents)
        hits = store.search(scope, TextQuery(text="recall", filters=expr, top_k=limit))
    else:
        store = InMemoryVectorStore()
        records = [VectorRecord(str(index), [1.0, 0.0], item)
                   for index, item in enumerate(metadata)]
        store.insert(scope, records)
        query = VectorQuery(vector=[1.0, 0.0], filters=expr, top_k=limit)
        if backend == "vector-recall":
            hits = store.recall(scope, query)
        else:
            hits = store.search(scope, query)
    return [hit.id for hit in hits]


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
def test_filter_excludes_earlier_equal_score_rows_before_limit(backend) -> None:
    metadata = [
        {"hierarchy_kind": "topic"},
        {"user_metadata.hierarchy_kind": "time"},
        {"hierarchy_kind": "time"},
    ]

    result = search_ids(backend, metadata, FilterClause("hierarchy_kind", FilterOp.EQ, "time"), 1)

    assert result == ["2"]


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
def test_nested_boolean_filters_preserve_metadata_namespace(backend) -> None:
    metadata = [
        {"user_metadata.owner": "alice", "system_metadata.owner": "bob"},
        {"user_metadata.owner": "bob", "system_metadata.owner": "alice"},
        {"user_metadata.owner": "alice", "system_metadata.owner": "alice"},
    ]
    expr = FilterGroup(FilterLogic.AND, [
        FilterClause("system_metadata.owner", FilterOp.EQ, "alice"),
        FilterGroup(FilterLogic.NOT, [FilterClause("user_metadata.owner", FilterOp.EQ, "bob")]),
    ])

    assert search_ids(backend, metadata, expr) == ["2"]


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
@pytest.mark.parametrize("case", [
    (FilterOp.EQ, "x", ["1"]),
    (FilterOp.IN, ["x"], ["1"]),
    (FilterOp.CONTAINS, "x", ["0"]),
    (FilterOp.NE, "x", ["0", "2"]),
    (FilterOp.NOT_IN, ["x"], ["0", "2"]),
    (FilterOp.GTE, 1, []),
])
def test_array_scalar_and_incomparable_types_keep_shared_semantics(backend, case) -> None:
    operation, value, expected = case
    metadata = [{"user_metadata.tag": ["x"]}, {"user_metadata.tag": "x"}, {}]

    result = search_ids(backend, metadata, FilterClause("user_metadata.tag", operation, value))

    assert result == expected


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
def test_or_range_and_missing_field_behavior_are_shared(backend) -> None:
    metadata = [
        {"tree_span_start": 1, "tree_span_end": 4},
        {"tree_span_start": 9, "tree_span_end": 10},
        {"tree_span_start": [1], "tree_span_end": [10]},
        {"user_metadata.allowed": True},
    ]
    expr = FilterGroup(FilterLogic.OR, [
        FilterGroup(FilterLogic.AND, [
            FilterClause("tree_span_start", FilterOp.LTE, 4),
            FilterClause("tree_span_end", FilterOp.GTE, 4),
        ]),
        FilterClause("user_metadata.allowed", FilterOp.EQ, True),
    ])

    assert search_ids(backend, metadata, expr) == ["0", "3"]


@pytest.mark.parametrize("backend", ["fulltext", "vector-search", "vector-recall"])
def test_no_filter_preserves_original_stable_order(backend) -> None:
    assert search_ids(backend, [{}, {}, {}], None, 2) == ["0", "1"]


@pytest.mark.parametrize(("field", "value"), [
    ("hierarchy_kind", "time"), ("hierarchy_role", "snapshot"),
    ("hierarchy_status", "active"), ("tree_parent_id", "parent"),
    ("tree_span_start", 0), ("tree_span_end", 0), ("id", "leaf"),
])
def test_tree_filter_reads_canonical_truth_instead_of_metadata(field, value) -> None:
    from datetime import UTC, datetime

    from jiuwen_memory.common.type_def import (
        HierarchyKind,
        HierarchyRef,
        HierarchyRole,
        MemoryUnit,
        matches_memory_unit,
        normalize,
    )
    unit = MemoryUnit(id="leaf", hierarchy=HierarchyRef(
        kind=HierarchyKind.TIME, role=HierarchyRole.SNAPSHOT, parent_id="parent",
        span_start=datetime(1970, 1, 1, tzinfo=UTC),
        span_end=datetime(1970, 1, 1, tzinfo=UTC),
    ), user_metadata={field: "business"}, system_metadata={field: "stale"})
    assert matches_memory_unit(unit, normalize({field: value}))
    assert matches_memory_unit(unit, normalize({f"user_metadata.{field}": "business"}))
    assert matches_memory_unit(unit, normalize({f"system_metadata.{field}": "stale"}))
