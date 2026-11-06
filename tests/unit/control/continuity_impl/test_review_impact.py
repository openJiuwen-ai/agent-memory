"""Impact propagation: bounded forward dependency review, three-tier aggregation."""

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import RelationStatus
from jiuwen_memory.control.continuity_impl.review import review_impact
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.types import Edge, Node

pytestmark = pytest.mark.unit

SCOPE = Scope(org="o", space="s")


def _graph(edges):
    graph = InMemoryGraphStore()
    nodes = {n for pair in edges.values() for n in pair}
    graph.insert(
        SCOPE,
        nodes=[Node(n) for n in nodes],
        edges=[Edge(eid, s, t, "supports") for eid, (s, t) in edges.items()],
    )
    return graph


def _resolve(edges):
    def resolve(edge_id):
        endpoints = edges.get(edge_id)
        assert endpoints is not None, f"Unexpected edge ID: {edge_id}"
        s, t = endpoints
        return (
            SimpleNamespace(
                source=SimpleNamespace(key=s),
                target=SimpleNamespace(key=t),
                relation_type="supports",
                status=RelationStatus.CONFIRMED,
            ),
            None,
        )

    return resolve


def test_only_dependent_subgraph_is_affected():
    edges = {"e1": ("a", "b"), "e2": ("b", "c"), "e3": ("a", "d"), "e4": ("x", "y")}
    result = review_impact(
        SCOPE, _graph(edges), _resolve(edges), "a", required={"c"}
    )
    assert set(result["required"]) == {"c"}
    assert set(result["candidate"]) == {"b", "d"}
    assert result["gaps"] == []
    assert "x" not in result["candidate"]
    assert "y" not in result["candidate"]


def test_three_tiers_preserve_references():
    edges = {"e1": ("a", "b"), "e2": ("b", "c"), "e3": ("a", "d")}
    result = review_impact(
        SCOPE, _graph(edges), _resolve(edges), "a", required={"b", "c"}
    )
    assert set(result["required"]) == {"b", "c"}
    assert set(result["candidate"]) == {"d"}


def test_node_overflow_reports_gap():
    edges = {"e1": ("a", "b"), "e2": ("b", "c"), "e3": ("c", "d")}
    result = review_impact(
        SCOPE, _graph(edges), _resolve(edges), "a", required=set(), max_nodes=2
    )
    assert result["gaps"] == ["unchecked_dependencies"]


def test_depth_overflow_reports_gap():
    edges = {"e1": ("a", "b"), "e2": ("b", "c"), "e3": ("c", "d")}
    result = review_impact(
        SCOPE, _graph(edges), _resolve(edges), "a", required=set(), max_depth=1
    )
    assert result["gaps"] == ["unchecked_dependencies"]


def test_unconfirmed_edges_do_not_propagate():
    edges = {"e1": ("a", "b")}

    def resolve(edge_id):
        endpoints = edges.get(edge_id)
        assert endpoints is not None, f"Unexpected edge ID: {edge_id}"
        s, t = endpoints
        return (
            SimpleNamespace(
                source=SimpleNamespace(key=s),
                target=SimpleNamespace(key=t),
                relation_type="supports",
                status=RelationStatus.NEEDS_REVIEW,
            ),
            None,
        )

    result = review_impact(SCOPE, _graph(edges), resolve, "a", required=set())
    assert result["required"] == []
    assert result["candidate"] == []
    assert result["gaps"] == []


def test_requires_propagates_reversely():
    edges = {"e1": ("a", "b")}  # a requires b

    def resolve(edge_id):
        endpoints = edges.get(edge_id)
        assert endpoints is not None, f"Unexpected edge ID: {edge_id}"
        s, t = endpoints
        return (
            SimpleNamespace(
                source=SimpleNamespace(key=s),
                target=SimpleNamespace(key=t),
                relation_type="requires",
                status=RelationStatus.CONFIRMED,
            ),
            None,
        )

    # A change at b (the required prerequisite) affects a (the dependent source).
    result = review_impact(SCOPE, _graph(edges), resolve, "b", required=set())
    assert set(result["candidate"]) == {"a"}
    assert result["gaps"] == []


def test_supports_does_not_propagate_reversely():
    edges = {"e1": ("a", "b")}  # a supports b

    def resolve(edge_id):
        endpoints = edges.get(edge_id)
        assert endpoints is not None, f"Unexpected edge ID: {edge_id}"
        s, t = endpoints
        return (
            SimpleNamespace(
                source=SimpleNamespace(key=s),
                target=SimpleNamespace(key=t),
                relation_type="supports",
                status=RelationStatus.CONFIRMED,
            ),
            None,
        )

    # A change at b (the target of supports) is not a forward dependency of b.
    result = review_impact(SCOPE, _graph(edges), resolve, "b", required=set())
    assert result["candidate"] == []
    assert result["gaps"] == []
