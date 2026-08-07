"""Recall channel tests for keyword, vector, and graph recall."""

from __future__ import annotations

import pytest

from common.errors import ValidationError
from retrieval.recaller_impl.graph_recaller import GraphRecaller
from retrieval.recaller_impl.keyword_recaller import KeywordRecaller
from retrieval.recaller_impl.vector_recaller import VectorRecaller
from retrieval.types import ParsedQuery, RetrievalQuery
from storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from storage.storage_impl.composite_storage import CompositeStorage
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
    storage = CompositeStorage(vector=indexed_world.vector)
    strict = VectorRecaller(storage, min_similarity=top + 0.01)
    assert strict.recall(scope, parsed, 10) == []

    # 阈值低于最高分 → 至少保留最相关的那条。
    loose = VectorRecaller(storage, min_similarity=top - 0.01)
    kept = loose.recall(scope, parsed, 10)
    assert kept and kept[0].unit_id == baseline[0].unit_id


class _LowerIsBetterStore:
    """契约桩：按 VectorStore 接口声明分数为距离型（越小越相关）。"""

    @staticmethod
    def score_higher_is_better() -> bool:
        return False


def test_vector_recaller_rejects_lower_is_better_metric() -> None:
    # MaxP 与融合统一要求高分优先；距离型度量无论是否开阈值都拒绝。
    with pytest.raises(ValidationError):
        VectorRecaller(CompositeStorage(vector=_LowerIsBetterStore()), min_similarity=0.5)

    with pytest.raises(ValidationError):
        VectorRecaller(CompositeStorage(vector=_LowerIsBetterStore()), min_similarity=0.0)


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
    recaller = GraphRecaller(CompositeStorage(graph=graph))

    results = recaller.recall(scope, ParsedQuery(raw="coffee", keywords=["coffee"]), 10)

    assert "B" in {result.unit_id for result in results}


# ---------------------------------------------------------------------------
# L0/L1 分层召回（store 为 None 时跳过；layer 参数正确）
# ---------------------------------------------------------------------------


def test_vector_recaller_layer_none_store_returns_empty(scope) -> None:
    """L0/L1 recaller store 未注入（None）→ recall 返空，不报错。"""
    storage = CompositeStorage()
    recaller = VectorRecaller(storage, layer="l0")
    parsed = ParsedQuery(raw="x", vector=[0.1, 0.2, 0.3])
    assert recaller.recall(scope, parsed, 10) == []

    recaller_l1 = VectorRecaller(storage, layer="l1")
    assert recaller_l1.recall(scope, parsed, 10) == []


def test_keyword_recaller_layer_none_store_returns_empty(scope) -> None:
    """L0/L1 keyword recaller store 未注入 → recall 返空。"""
    recaller = KeywordRecaller(CompositeStorage(), layer="l0")
    parsed = ParsedQuery(raw="x", keywords=["x"])
    assert recaller.recall(scope, parsed, 10) == []


def test_vector_recaller_layer_param_set() -> None:
    """layer 参数正确传入（l2/l0/l1）。"""
    storage = CompositeStorage()
    r_l2 = VectorRecaller(storage, layer="l2")
    r_l0 = VectorRecaller(storage, layer="l0")
    r_l1 = VectorRecaller(storage, layer="l1")
    assert r_l2.layer == "l2"
    assert r_l0.layer == "l0"
    assert r_l1.layer == "l1"


def test_keyword_recaller_layer_param_set() -> None:
    """layer 参数正确传入。"""
    storage = CompositeStorage()
    assert KeywordRecaller(storage, layer="l2").layer == "l2"
    assert KeywordRecaller(storage, layer="l0").layer == "l0"
    assert KeywordRecaller(storage, layer="l1").layer == "l1"
