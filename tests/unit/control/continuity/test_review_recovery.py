"""Crash recovery, association diagnostics and conditional evidence semantics."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    MemoryRef,
    RelationRecord,
    RoleViewQuery,
    TaskRequest,
)
from jiuwen_memory.control.continuity.repository import ContinuityRepository
from jiuwen_memory.control.continuity.service import ContinuityService
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit


def bound_event(service, scope, read):
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)
    bound = service.task(
        scope,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="local"),
        "owner", read,
    )
    return ContinuityEvent(
        task["task_id"], bound["task"]["bindings"][0]["binding_id"], "event", "observation"
    )


def test_partial_receipt_recovers_after_task_commit_and_restart(setup, monkeypatch):
    service, scope, write, read, _, units = setup
    event = bound_event(service, scope, read)
    save = service.repository.save

    def fail_final_receipt(scope, kind, key, record):
        if kind == "events" and record["status"] == "committed":
            raise OSError("simulated crash before receipt commit")
        save(scope, kind, key, record)

    monkeypatch.setattr(service.repository, "save", fail_final_receipt)
    with pytest.raises(OSError):
        service.record(scope, event, "wb", read, write)
    assert service.repository.list(scope, "events")[0]["status"] == "partial"
    assert len(service.repository.get(scope, "tasks", event.task_id)["background"]) == 1
    service.repository.kv.close()
    reopened = SQLiteKVStore(service.repository.kv._fallback_db_path)
    try:
        restarted = ContinuityService(
            ContinuityRepository(reopened, single_writer=True), service.graph, RoleViewBuilder(),
            projector=service.projector,
        )
        result = restarted.record(scope, event, "wb", read, write)
        assert result["status"] == "committed"
        assert restarted.repository.list(scope, "events")[0]["status"] == "committed"
        assert len(restarted.repository.get(scope, "tasks", event.task_id)["background"]) == 1
        assert len(units) == 1, "retry must not write the original content again"
    finally:
        reopened.close()


@pytest.mark.parametrize("error,level", [(ValidationError("secret"), "warning"),
                                         (TypeError("secret"), "error")])
def test_association_failure_is_observable_and_event_stays_committed(
    setup, monkeypatch, error, level
):
    from jiuwen_memory.control.continuity import service as module

    service, scope, write, read, _, units = setup
    logger = Mock()
    monkeypatch.setattr(module, "logger", logger)
    service.associator = Mock()
    service.associator.associate.side_effect = error
    event = bound_event(service, scope, read)
    result = service.record(scope, event, "wb", read, write)
    assert result["status"] == "committed"
    assert result["association_status"] == "partial"
    getattr(logger, level).assert_called_once_with(
        "continuity association incomplete; error_type=%s", type(error).__name__
    )
    service.associator.associate.side_effect = None
    service.associator.associate.return_value = []
    assert service.record(scope, event, "wb", read, write)["association_status"] == "committed"
    assert len(units) == 1


def view(service, scope, read, anchor):
    return service.view_builder.build(
        scope, RoleViewQuery(anchor=anchor), graph=service.graph, read=read,
        discover=lambda q, n: [], resolve_edge=lambda rid: service.resolve_edge(scope, rid, read),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_conditional_support_is_direction_independent_and_revocable(setup, reverse):
    service, scope, write, read, _, _ = setup
    judgment = MemoryRef.of(write("pool is exhausted", {"continuity_kind": "judgment"})[0])
    evidence = MemoryRef.of(write("pool metrics")[0])
    relation = RelationRecord(
        "support", evidence, judgment, "supports", status="confirmed",
        evidence_refs=[evidence], conditions=["version=2", "load=100"],
    )
    service.relation(scope, relation, 0, read, write)
    anchor = judgment if reverse else evidence
    result = view(service, scope, read, anchor)
    item = result["judgments"][0]
    assert item["status"] == "supported_in_conditions"
    assert item["conditions"] == relation.conditions
    assert item["path_ids"]
    assert len(item["evidence_refs"]) == 2
    assert result["meta"]["completeness"] == "partial"
    assert result["meta"]["unchecked_scope"]
    service.relation(scope, replace(relation, status="revoked", revision=2), 1, read, write)
    assert view(service, scope, read, judgment)["judgments"][0]["status"] == "reported"


@pytest.mark.parametrize("kind,status,conditions", [
    ("supports", "candidate", ["v2"]),
    ("supports", "confirmed", []),
    ("verification_method_for", "confirmed", ["v2"]),
    ("verifies", "confirmed", ["v2"]),
])
def test_confirming_a_relation_does_not_automatically_confirm_a_judgment(
    setup, kind, status, conditions
):
    service, scope, write, read, _, _ = setup
    judgment = MemoryRef.of(write("hypothesis", {"continuity_kind": "judgment"})[0])
    evidence = MemoryRef.of(write("source")[0])
    service.relation(scope, RelationRecord(
        "r", evidence, judgment, kind, status=status,
        evidence_refs=[evidence], conditions=conditions,
    ), 0, read, write)
    assert view(service, scope, read, judgment)["judgments"][0]["status"] == "reported"


@pytest.mark.parametrize("resolution,expected", [("", "needs_review"), ("prefer_a", "refuted")])
def test_contradiction_takes_precedence_over_support(setup, resolution, expected):
    service, scope, write, read, _, _ = setup
    judgment = MemoryRef.of(write("hypothesis", {"continuity_kind": "judgment"})[0])
    evidence = MemoryRef.of(write("evidence")[0])
    for relation in [
        RelationRecord("support", evidence, judgment, "supports", status="confirmed",
                       evidence_refs=[evidence], conditions=["v2"]),
        RelationRecord("conflict", evidence, judgment, "contradicts", status="confirmed",
                       evidence_refs=[evidence], conditions=["v2"], resolution=resolution,
                       conflict_kind="factual", resolution_reason="reviewed",
                       resolution_origin="human" if resolution else ""),
    ]:
        service.relation(scope, relation, 0, read, write)
    assert view(service, scope, read, judgment)["judgments"][0]["status"] == expected
