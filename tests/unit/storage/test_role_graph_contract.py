"""Reusable evidence graph contracts; no external database required."""
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.graph_impl.kv_graph_store import KVGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

pytestmark = pytest.mark.unit


@pytest.fixture
def graph(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "graph.db"))
    yield KVGraphStore(kv)
    kv.close()


@pytest.fixture
def scope():
    return Scope(org="org", space="project")


def test_parallel_edges_preserve_direction_and_identity(graph, scope):
    edges = [Edge("support", "a", "b", "supports"),
             Edge("conflict", "a", "b", "contradicts"),
             Edge("reverse", "b", "a", "requires")]
    graph.insert(scope, nodes=[Node("a"), Node("b")], edges=edges)
    for endpoint in ("a", "b"):
        assert {e.id: e for e in graph.incident_edges(scope, endpoint)} == {e.id: e for e in edges}
    assert len(graph.incident_edges(scope, "a", limit=1)) == 1


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_graph_isolation_on_every_scope_dimension(graph, scope, dimension):
    other = replace(scope, **{dimension: "other"})
    graph.insert(scope, nodes=[Node("a", properties={"content": "needle"}), Node("b")],
                 edges=[Edge("edge", "a", "b")])
    assert graph.get(other, ["a"]) == []
    assert graph.seed_ids(other, {"needle"}) == []
    assert graph.incident_edges(other, "a") == []
    graph.delete(other, node_ids=["a"], edge_ids=["edge"])
    assert len(graph.incident_edges(scope, "a")) == 1


def test_seeding_does_not_traverse_and_update_refreshes_content(graph, scope):
    graph.insert(scope, nodes=[Node("a", properties={"content": "needle"}), Node("b")],
                 edges=[Edge("edge", "a", "b")])
    assert graph.seed_ids(scope, {"needle"}) == ["a"]
    assert graph.seed_ids(scope, set()) == []
    assert [n.id for n in graph.search(scope, GraphQuery("a"))] == ["b"]
    graph.update(scope, nodes=[Node("a", properties={"content": "replacement"})])
    assert graph.seed_ids(scope, {"needle"}) == []
    assert graph.seed_ids(scope, {"replacement"}) == ["a"]


def test_delete_node_preserves_unrelated_edges(graph, scope):
    graph.insert(scope, nodes=[Node(n) for n in "abc"],
                 edges=[Edge("ab", "a", "b"), Edge("bc", "b", "c")])
    graph.delete(scope, node_ids=["a"])
    graph.delete(scope, node_ids=["a"])
    assert [e.id for e in graph.incident_edges(scope, "b")] == ["bc"]
    assert graph.get(scope, ["a"]) == []


def test_plain_insert_conflicts_and_missing_update_fails(graph, scope):
    graph.insert(scope, nodes=[Node("a")])
    with pytest.raises(ConflictError):
        graph.insert(scope, nodes=[Node("a")])
    with pytest.raises(NotFoundError):
        graph.update(scope, nodes=[Node("missing")])
