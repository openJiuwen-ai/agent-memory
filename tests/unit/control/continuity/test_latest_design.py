"""Latest-design acceptance through the application and storage interfaces."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.common.relations.catalog import RelationCatalog
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    RelationRecord,
    RoleViewQuery,
    TaskRequest,
)
from jiuwen_memory.construction.conflict_resolver_impl.strategies import (
    RecencyResolver,
    ResolverChain,
    VerificationResolver,
)
from jiuwen_memory.storage.graph_impl.kv_graph_store import KVGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore
from jiuwen_memory.storage.types import Edge, Node

pytestmark = pytest.mark.unit


def _task(service, scope, read):
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)
    bound = service.task(
        scope,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="context"),
        "owner",
        read,
    )
    return task["task_id"], bound["task"]["bindings"][0]["binding_id"]


def test_custom_policy_and_symmetric_mandatory_counterevidence(setup):
    service, scope, write, read, _, _ = setup
    catalog = RelationCatalog(
        {"mitigates": {"display_name": "缓解", "description": "A mitigates B"}},
        {"security": {"traversal": [{"edge_type": "mitigates", "direction": "backward"}]}},
    )
    service.catalog = service.view_builder.catalog = catalog
    problem, solution, opposite = [MemoryRef.of(write(t)[0]) for t in ("problem", "fix", "counter")]
    for rid, a, b, kind in [
        ("fix", solution, problem, "mitigates"),
        ("counter", problem, opposite, "contradicts"),
    ]:
        service.relation(
            scope,
            RelationRecord(rid, a, b, kind, status="confirmed", evidence_refs=[a]),
            0,
            read,
            write,
        )
    result = service.view(
        scope, RoleViewQuery(anchor=problem, duty="security"), read, lambda *_: []
    )
    assert {p["steps"][-1]["relation_id"] for p in result["paths"]} == {"fix", "counter"}
    assert result["meta"]["policy_version"] == catalog.version
    with pytest.raises(ValidationError, match="unknown duty"):
        service.view(
            scope, RoleViewQuery(anchor=problem, duty="not_registered"), read, lambda *_: []
        )
    with pytest.raises(ValidationError):
        RelationCatalog({"supports": {"symmetric": True}})


def test_write_ack_loss_reconciles_without_repeating_body(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    event = ContinuityEvent(task, binding, "one", "record exactly once")

    def interrupted(content, metadata):
        write(content, metadata)
        raise RuntimeError("connection closed after commit")

    unknown = service.record(scope, event, "wb", read, interrupted)
    assert unknown["status"] == "unknown"
    assert service.record(scope, event, "wb", read, write)["status"] == "unknown"
    recovered = service.recover(
        scope,
        ContinuityRecovery(
            "event", unknown["operation_id"], [MemoryRef.of(next(iter(units.values())))]
        ),
        read,
    )
    assert recovered["status"] == "partial"
    assert service.record(scope, event, "wb", read, write)["status"] == "committed"
    assert len(units) == 1


def test_concurrent_registry_mutations_have_one_winner(setup):
    service, scope, _, read, _, _ = setup
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)

    def bind(n):
        try:
            service.task(
                scope,
                TaskRequest(
                    "bind", str(n), task["task_id"], 1, connector_id="wb", context_id=str(n)
                ),
                "owner",
                read,
            )
            return "ok"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bind, range(4)))
    assert results.count("ok") == 1
    assert results.count("conflict") == 3


def test_verification_resolution_and_manual_review_survive_revision(setup):
    service, scope, write, read, _, _ = setup
    service.conflict_resolver = ResolverChain([VerificationResolver(), RecencyResolver()])
    a, b, proof = [MemoryRef.of(write(t)[0]) for t in ("16GB", "32GB", "measured 32GB")]
    service.relation(
        scope,
        RelationRecord(
            "verify",
            proof,
            b,
            "verifies",
            status="confirmed",
            conditions=["host A"],
            evidence_refs=[proof],
        ),
        0,
        read,
        write,
    )
    conflict = RelationRecord(
        "conflict",
        a,
        b,
        "contradicts",
        status="confirmed",
        conditions=["host A"],
        evidence_refs=[proof],
        conflict_kind="factual",
    )
    service.relation(scope, conflict, 0, read, write)
    resolved, _ = service.resolve_edge(scope, "conflict", read)
    assert resolved.resolution == "prefer_b"
    view = service.view(scope, RoleViewQuery(anchor=a), read, lambda *_: [])
    assert next(item for item in view["facts"] if item["text"] == "16GB")["status"] == "refuted"
    review = replace(
        conflict,
        revision=2,
        resolution="both_valid",
        resolution_reason="different virtual machines",
    )
    service.relation(scope, review, 1, read, write)
    service.relation(scope, replace(conflict, revision=3), 2, read, write)
    current, _ = service.resolve_edge(scope, "conflict", read)
    assert current.resolution == "both_valid"
    assert current.resolution_origin == "human"


def test_advisory_never_chooses_winner(setup):
    service, scope, write, read, _, _ = setup
    service.conflict_resolver = ResolverChain([VerificationResolver(), RecencyResolver()])
    a, b = [MemoryRef.of(write(t)[0]) for t in ("continue experiment", "do not deploy")]
    service.relation(
        scope,
        RelationRecord(
            "advice",
            a,
            b,
            "contradicts",
            status="confirmed",
            evidence_refs=[a],
            conflict_kind="advisory",
        ),
        0,
        read,
        write,
    )
    relation, _ = service.resolve_edge(scope, "advice", read)
    assert relation.resolution == "defer"
    assert relation.resolution_origin == "automatic"


def test_explicit_legacy_background_requires_no_duty_or_semantic_search(setup):
    service, scope, write, read, _, _ = setup
    old = write("old JiuwenSwarm observation without task or duty tags")[0]
    ref = MemoryRef.of(old)
    task = service.task(scope, TaskRequest("create", "create", background=[ref]), "owner", read)
    result = service.view(scope, RoleViewQuery(task_id=task["task_id"]), read, lambda *_: [])
    assert result["facts"][0]["text"] == old.content
    assert result["facts"][0]["discovery"] == "background"
    assert MemoryRef.of(old) == ref
    assert result["meta"]["relation_coverage"] == "unknown"


def test_persistent_graph_moved_edge_and_scope_isolation(tmp_path):
    from jiuwen_memory.api import Scope

    scope, other = Scope(org="o", space="s"), Scope(org="o", space="other")
    path = str(tmp_path / "graph.db")
    kv = SQLiteKVStore(path)
    graph = KVGraphStore(kv)
    graph.insert(
        scope,
        nodes=[Node(n, "memory") for n in ("a", "b", "c")],
        edges=[Edge("e", "a", "b", "supports")],
    )
    graph.update(scope, edges=[Edge("e", "b", "c", "supports")])
    graph.delete(scope, node_ids=["a"])
    kv.close()
    reopened = SQLiteKVStore(path)
    restored = KVGraphStore(reopened)
    assert restored.incident_edges(scope, "b")[0].target == "c"
    assert restored.incident_edges(other, "b") == []
    reopened.close()


def test_second_durable_registry_writer_is_rejected(tmp_path):
    from jiuwen_memory.control.continuity.repository import ContinuityRepository

    path = str(tmp_path / "single.db")
    first, second = SQLiteKVStore(path), SQLiteKVStore(path)
    try:
        ContinuityRepository(first, single_writer=True)
        with pytest.raises(ConflictError):
            ContinuityRepository(second, single_writer=True)
        first.close()
        ContinuityRepository(second, single_writer=True)
    finally:
        first.close()
        second.close()


def test_low_confidence_classification_does_not_become_a_result():
    event = ContinuityEvent(
        "t", "b", "e", "might work", kind="result", classification_confidence="low"
    )
    assert event.kind == "unclassified"
    assert asdict(event)["classification_confidence"] == "low"


def test_resolution_is_invalidated_when_verification_is_revoked(setup):
    service, scope, write, read, _, _ = setup
    service.conflict_resolver = ResolverChain([VerificationResolver()])
    a, b, evidence = [MemoryRef.of(write(t)[0]) for t in ("first", "second", "test result")]
    proof = RelationRecord(
        "proof",
        evidence,
        b,
        "verifies",
        status="confirmed",
        evidence_refs=[evidence],
        conditions=["env"],
    )
    service.relation(scope, proof, 0, read, write)
    conflict = RelationRecord(
        "conflict",
        a,
        b,
        "contradicts",
        status="confirmed",
        evidence_refs=[a, b],
        conditions=["env"],
        conflict_kind="factual",
    )
    service.relation(scope, conflict, 0, read, write)
    resolved, _ = service.resolve_edge(scope, "conflict", read)
    assert resolved.resolution == "prefer_b"
    assert len(resolved.resolution_refs) == 1
    service.relation(scope, replace(proof, revision=2, status="revoked"), 1, read, write)
    with pytest.raises(ConflictError, match="resolution evidence changed"):
        service.resolve_edge(scope, "conflict", read)
    view = service.view(scope, RoleViewQuery(anchor=a), read, lambda *_: [])
    assert all(item["status"] != "refuted" for item in view["facts"])
    assert any(gap["blocking"] for gap in view["gaps"])


def test_relation_projection_does_not_delete_unrelated_graph_edge(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("a", "b")]
    service.graph.insert(scope, edges=[Edge("r", "external-a", "external-b", "similar_to")])
    relation = RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a])
    service.relation(scope, relation, 0, read, write)
    service.relation(scope, replace(relation, revision=2, status="revoked"), 1, read, write)
    assert service.graph.incident_edges(scope, "external-a")[0].id == "r"


def test_required_stale_snapshot_is_not_satisfied_by_optional_current_snapshot(setup):
    service, scope, write, read, _, _ = setup
    current = MemoryRef.of(write("current content")[0])
    stale = replace(current, content_digest="0" * 64)
    result = service.view_builder.build(
        scope,
        RoleViewQuery(task_id="t"),
        graph=service.graph,
        read=read,
        discover=lambda *_: [],
        resolve_edge=lambda rid: service.resolve_edge(scope, rid, read),
        background=[
            {"ref": asdict(current), "required": False},
            {"ref": asdict(stale), "required": True},
        ],
    )
    assert any(g["code"] == "reference_unavailable" and g["blocking"] for g in result["gaps"])


def test_legacy_cause_mapping_reverses_direction_without_confirmation(setup):
    from jiuwen_memory.common.type_def import Relation
    from jiuwen_memory.construction.relations.legacy import evidence_candidate

    _, _, write, _, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("effect", "cause")]
    relation = evidence_candidate(
        Relation("a", "b", "caused_by"), {"a": a, "b": b}, relation_id="legacy-cause"
    )
    assert relation.source == b and relation.target == a
    assert relation.relation_type == "possible_cause_of"
    assert relation.status.value == "candidate"
    assert evidence_candidate(Relation("a", "b", "similar_to"), {}, relation_id="ignored") is None
    with pytest.raises(ValidationError):
        evidence_candidate(Relation("a", "b", "refers_to"), {}, relation_id="unmapped")
