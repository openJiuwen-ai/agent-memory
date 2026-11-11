"""Real Neo4j acceptance; skipped environment is explicitly NOT a delivery pass.

Set F10_NEO4J_URI, F10_NEO4J_USER, F10_NEO4J_PASSWORD and optionally F10_NEO4J_DATABASE.
Every test uses a fresh organization namespace; cleanup never touches other namespaces.
"""
import os
from uuid import uuid4

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.graph_impl.neo4j_graph_store import Neo4jGraphStore
from jiuwen_memory.storage.types import Edge, GraphQuery, Node

pytestmark = pytest.mark.integration


@pytest.fixture
def backend():
    uri = os.environ.get("F10_NEO4J_URI")
    if not uri:
        pytest.skip("real Neo4j not configured: F10_NEO4J_URI is required for acceptance")
    params = dict(database=os.environ.get("F10_NEO4J_DATABASE", "neo4j"),
                  username=os.environ.get("F10_NEO4J_USER"),
                  password=os.environ.get("F10_NEO4J_PASSWORD"))
    scope = Scope(org=f"f10-test-{uuid4().hex}", space="project")
    store = Neo4jGraphStore(uri, **params)
    store.health()
    try:
        yield store, scope, uri, params
    finally:
        cleanup = Neo4jGraphStore(uri, **params)
        try:
            # White-box fixture/fault injection; do not expose a production test API.
            # pylint: disable-next=protected-access
            cleanup._run(
                "MATCH (n) WHERE n.scope_key=$sk DETACH DELETE n",
                # White-box fixture/fault injection; do not expose a production test API.
                # pylint: disable-next=protected-access
                sk=cleanup._scope_key(scope),
            )
        finally:
            cleanup.close()
            store.close()


def test_direction_parallel_edges_and_recovery_after_reopen(backend):
    store, scope, uri, params = backend
    nodes = [Node("a", properties={"content": "needle"}), Node("b"), Node("c")]
    edge = Edge("support", "a", "b", "supports")
    store.project_edge(scope, edge, revision=1, nodes=nodes)
    store.project_edge(scope, edge, revision=1, nodes=nodes)
    store.project_edge(scope, Edge("reverse", "b", "a", "requires"), revision=1)
    assert {(e.id, e.source, e.target) for e in store.incident_edges(scope, "a")} == {
        ("support", "a", "b"), ("reverse", "b", "a")
    }
    assert store.seed_ids(scope, {"needle"}) == ["a"]
    assert [node.id for node in store.search(scope, GraphQuery("a", depth=2))] == ["b"]
    store.unproject_edge(scope, "support", revision=3)
    store.close()
    reopened = Neo4jGraphStore(uri, **params)
    try:
        reopened.health()
        with pytest.raises(ConflictError):
            reopened.project_edge(scope, edge, revision=2)
        assert [e.id for e in reopened.incident_edges(scope, "a")] == ["reverse"]
        assert reopened.get(Scope(org=scope.org, space="other"), ["a"]) == []
    finally:
        reopened.close()


def test_failed_batch_and_endpoint_move_roll_back(backend):
    store, scope, _, _ = backend
    store.insert(scope, nodes=[Node("a"), Node("b")], edges=[Edge("r", "a", "b")])
    with pytest.raises(NotFoundError):
        store.update(scope, edges=[Edge("r", "missing", "b")])
    assert [(e.source, e.target) for e in store.incident_edges(scope, "a")] == [("a", "b")]
    with pytest.raises(NotFoundError):
        store.insert(scope, nodes=[Node("temporary")], edges=[Edge("bad", "absent", "b")])
    assert store.get(scope, ["temporary"]) == []
