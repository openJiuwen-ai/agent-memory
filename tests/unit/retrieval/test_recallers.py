"""Recall channel tests for keyword, vector, and graph recall."""

from __future__ import annotations

import pytest

from retrieval.recaller_impl.graph_recaller import GraphRecaller
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
