"""Recall channel tests for keyword, vector, and graph recall."""

from __future__ import annotations

import pytest

from common.errors import ValidationError
from retrieval.recaller_impl.graph_recaller import GraphRecaller
from retrieval.recaller_impl.vector_recaller import VectorRecaller
from retrieval.types import ParsedQuery, RetrievalQuery
from storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from storage.types import Edge, Node

pytestmark = pytest.mark.unit


@pytest.fixture
def indexed_world(world, unit_factory, index_unit_fn):
    index_unit_fn(world, unit_factory("u1", "alice likes coffee"))
    index_unit_fn(world, unit_factory("u2", "bob likes tea"))
    return world


def test_keyword_recall_matches_token(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))

    ids = {result.unit_id for result in indexed_world.keyword.recall(scope, parsed, 10)}

    assert "u1" in ids
    assert "u2" not in ids


def test_vector_recall_ranks_relevant_first(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))

    results = indexed_world.vector_recaller.recall(scope, parsed, 10)

    assert results
    assert results[0].unit_id == "u1"


def test_vector_recall_empty_without_vector(world, scope) -> None:
    results = world.vector_recaller.recall(scope, ParsedQuery(raw="x"), 10)

    assert results == []


def test_vector_recall_min_similarity_filters(indexed_world, scope) -> None:
    parsed = indexed_world.parser.parse(RetrievalQuery(text="coffee"))
    baseline = indexed_world.vector_recaller.recall(scope, parsed, 10)
    assert baseline, "基线应有语义命中"
    top = baseline[0].score

    # 阈值高于最高分 → 全部砍掉（证明前置过滤生效）。
    strict = VectorRecaller(indexed_world.vector, min_similarity=top + 0.01)
    assert strict.recall(scope, parsed, 10) == []

    # 阈值低于最高分 → 至少保留最相关的那条。
    loose = VectorRecaller(indexed_world.vector, min_similarity=top - 0.01)
    kept = loose.recall(scope, parsed, 10)
    assert kept and kept[0].unit_id == baseline[0].unit_id


class _LowerIsBetterStore:
    """契约桩：按 VectorStore 接口声明分数为距离型（越小越相关）。"""

    @staticmethod
    def score_higher_is_better() -> bool:
        return False


def test_vector_min_similarity_rejects_lower_is_better_metric() -> None:
    # 距离型度量（越小越相关）+ 非零 min_similarity → 装配期直接拒绝（语义会反转）。
    with pytest.raises(ValidationError):
        VectorRecaller(_LowerIsBetterStore(), min_similarity=0.5)

    # min_similarity=0（默认关）不触发校验，任何度量都放行。
    VectorRecaller(_LowerIsBetterStore(), min_similarity=0.0)


def test_graph_recaller_returns_seed_neighbor(scope) -> None:
    graph = InMemoryGraphStore()
    graph.insert(
        scope,
        nodes=[
            Node(id="A", properties={"content": "coffee origin"}),
            Node(id="B", properties={"content": "latte recipe"}),
        ],
        edges=[Edge(id="e", source="A", target="B", relation="related")],
    )
    recaller = GraphRecaller(graph)

    results = recaller.recall(scope, ParsedQuery(raw="coffee", keywords=["coffee"]), 10)

    assert "B" in {result.unit_id for result in results}
