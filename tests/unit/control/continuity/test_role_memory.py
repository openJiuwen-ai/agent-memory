"""S10 behavior: role paths, revocation, retries and restart using real local stores."""

from dataclasses import asdict

import pytest

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    Duty,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    RoleViewQuery,
    TaskRequest,
)
from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
from jiuwen_memory.control.continuity.repository import ContinuityRepository
from jiuwen_memory.control.continuity.service import ContinuityService
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit


def test_same_anchor_different_duties_and_no_hidden_bridge(setup):
    service, scope, write, read, denied, _ = setup
    problem, cause, evidence, test = [
        MemoryRef.of(write(t)[0]) for t in ("timeout", "pool exhaustion", "metrics", "load test")
    ]
    for rid, source, target, kind in [
        ("cause", cause, problem, "possible_cause_of"),
        ("evidence", evidence, cause, "supports"),
        ("test", test, problem, "verification_method_for"),
    ]:
        service.relation(
            scope,
            RelationRecord(
                rid, source, target, kind, status=RelationStatus.CONFIRMED, evidence_refs=[evidence]
            ),
            0,
            read,
            write,
        )

    def view(duty):
        return service.view_builder.build(
            scope,
            RoleViewQuery(anchor=problem, duty=duty),
            graph=service.graph,
            read=read,
            discover=lambda q, n: [],
            resolve_edge=lambda rid: service.resolve_edge(scope, rid, read),
        )

    diagnosis = view(Duty.DIAGNOSIS)
    testing = view(Duty.TESTING)
    assert {p["steps"][-1]["relation_id"] for p in diagnosis["paths"]} == {"cause", "evidence"}
    assert {p["steps"][-1]["relation_id"] for p in testing["paths"]} == {"test"}
    denied.add(cause.unit_id)
    hidden = view(Duty.DIAGNOSIS)
    assert hidden["paths"] == []
    assert "pool exhaustion" not in str(hidden)
    assert "metrics" not in str(hidden), "must not traverse a hidden intermediate node"


def test_relation_retraction_checks_truth_even_with_stale_graph(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("hypothesis", "result")]
    rel = RelationRecord("r", b, a, "supports", status="confirmed", evidence_refs=[b])
    service.relation(scope, rel, 0, read, write)
    old_edge = service.graph.incident_edges(scope, a.key)[0]
    rel.revision = 2
    rel.status = RelationStatus.RETRACTED
    service.relation(scope, rel, 1, read, write)
    service.graph.insert(scope, edges=[old_edge])
    result = service.view_builder.build(
        scope,
        RoleViewQuery(anchor=a),
        graph=service.graph,
        read=read,
        discover=lambda q, n: [],
        resolve_edge=lambda rid: service.resolve_edge(scope, rid, read),
    )
    assert result["paths"] == []


def test_task_event_retries_and_restart_without_handoff(setup):
    service, scope, write, read, _, units = setup
    created = service.task(scope, TaskRequest("create", "create"), "owner", read)
    task_id = created["task_id"]
    bound = service.task(
        scope,
        TaskRequest("bind", "bind", task_id, 1, connector_id="wb", context_id="local"),
        "owner",
        read,
    )
    binding = bound["task"]["bindings"][0]["binding_id"]
    event = ContinuityEvent(task_id, binding, "event", "observed timeout")
    first = service.record(scope, event, "wb", read, write)
    assert service.record(scope, event, "wb", read, write)["refs"] == first["refs"]
    assert len(units) == 1
    service.repository.kv.close()
    reopened = SQLiteKVStore(service.repository.kv._fallback_db_path)
    restarted = ContinuityService(
        ContinuityRepository(reopened, single_writer=True),
        service.graph,
        RoleViewBuilder(),
        projector=EvidenceGraphBuilder(service.graph),
    )
    task = restarted.task(scope, TaskRequest("get", task_id=task_id), "reader", read)
    assert task["background"][0]["ref"] == first["refs"][0]
    with pytest.raises(ConflictError):
        restarted.record(
            scope, ContinuityEvent(task_id, binding, "event", "different"), "wb", read, write
        )
    assert asdict(read(MemoryRef.parse(first["refs"][0])).scope) == asdict(scope)
    reopened.close()
