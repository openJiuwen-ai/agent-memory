"""Conditional projection by revision: idempotent, conflict-safe, revocation-safe."""

import json
from dataclasses import asdict, replace

import pytest

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord
from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.graph_impl.kv_graph_store import KVGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.types import Edge, IndexWriteMode, Node

pytestmark = pytest.mark.unit


@pytest.fixture(params=["kv", "memory"])
def graph(request, tmp_path):
    if request.param == "kv":
        kv = SQLiteKVStore(str(tmp_path / "graph.db"))
        yield KVGraphStore(kv)
        kv.close()
    else:
        yield InMemoryGraphStore()


@pytest.fixture
def scope():
    return Scope(org="org", space="project")


def _edge(edge_id, revision, digest=""):
    return Edge(edge_id, "a", "b", "supports", {"digest": digest})


def test_same_revision_same_content_is_idempotent(graph, scope):
    graph.project_edge(scope, _edge("r", 2, "same"), revision=2, nodes=[Node("a"), Node("b")])
    graph.project_edge(scope, _edge("r", 2, "same"), revision=2, nodes=[Node("a"), Node("b")])
    incident = graph.incident_edges(scope, "a")
    assert [e.id for e in incident] == ["r"]
    assert incident[0].properties["revision"] == 2


def test_same_revision_different_content_conflicts(graph, scope):
    graph.project_edge(scope, _edge("r", 2, "one"), revision=2)
    with pytest.raises(ConflictError):
        graph.project_edge(scope, _edge("r", 2, "two"), revision=2)


def test_old_revision_does_not_overwrite_new(graph, scope):
    graph.project_edge(scope, _edge("r", 3, "new"), revision=3)
    with pytest.raises(ConflictError):
        graph.project_edge(scope, _edge("r", 2, "old"), revision=2)
    assert graph.incident_edges(scope, "a")[0].properties["digest"] == "new"


def test_revoke_at_newer_revision_blocks_old_replay(graph, scope):
    graph.project_edge(scope, _edge("r", 2, "x"), revision=2)
    graph.unproject_edge(scope, "r", revision=3)
    assert graph.incident_edges(scope, "a") == []
    with pytest.raises(ConflictError):
        graph.project_edge(scope, _edge("r", 2, "x"), revision=2)


def test_lower_revision_updates_in_place(graph, scope):
    graph.project_edge(scope, _edge("r", 1, "v1"), revision=1)
    graph.project_edge(scope, _edge("r", 2, "v2"), revision=2)
    incident = graph.incident_edges(scope, "a")
    assert incident[0].properties["revision"] == 2
    assert incident[0].properties["digest"] == "v2"


def test_unproject_is_idempotent(graph, scope):
    graph.project_edge(scope, _edge("r", 1), revision=1)
    graph.unproject_edge(scope, "r", revision=2)
    graph.unproject_edge(scope, "r", revision=2)
    assert graph.incident_edges(scope, "a") == []


def test_evidence_graph_replay_is_idempotent_and_revocable():
    scope = Scope(org="o", space="s")
    graph = InMemoryGraphStore()
    builder = EvidenceGraphBuilder(graph)
    a = MemoryUnit(id="a", scope=scope, segments=[Segment(content="A")])
    b = MemoryUnit(id="b", scope=scope, segments=[Segment(content="B")])
    ra, rb = MemoryRef.of(a), MemoryRef.of(b)
    relation = RelationRecord("r", ra, rb, "supports", status="confirmed", evidence_refs=[ra])

    def unit_for(record):
        return MemoryUnit(
            id="rel", scope=scope, segments=[Segment(content=json.dumps(asdict(record)))]
        )

    builder.build([unit_for(relation)], mode=IndexWriteMode.RETRIEVAL_ONLY)
    builder.build([unit_for(relation)], mode=IndexWriteMode.RETRIEVAL_ONLY)  # idempotent
    assert len(graph.incident_edges(scope, ra.key)) == 1
    revoked = replace(relation, revision=2, status="revoked")
    builder.build([unit_for(revoked)], mode=IndexWriteMode.RETRIEVAL_ONLY)
    assert graph.incident_edges(scope, ra.key) == []
