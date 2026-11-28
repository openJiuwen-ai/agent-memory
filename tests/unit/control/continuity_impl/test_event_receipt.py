"""Read-only event receipt probe (D8): status only, no content, no state change."""

import json
from dataclasses import asdict

import pytest

from jiuwen_memory.common.type_def.continuity import ContinuityEvent, MemoryRef, TaskRequest

pytestmark = pytest.mark.unit


def _bound_event(service, scope, write, read):
    task = service.task(scope, TaskRequest("create", "create"), "owner", read)
    bound = service.task(
        scope,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="session"),
        "owner",
        read,
    )
    binding = bound["task"]["bindings"][0]["binding_id"]
    return ContinuityEvent(task["task_id"], binding, "event", "source text"), binding


def _snapshot(service, scope):
    return json.dumps(
        {
            "events": service.repository.list(scope, "events"),
            "tasks": service.repository.list(scope, "tasks"),
        },
        sort_keys=True,
        default=str,
    )


def test_committed_receipt_is_reported_without_content(setup):
    service, scope, write, read, _, _ = setup
    event, binding = _bound_event(service, scope, write, read)
    committed = service.record(scope, event, "wb", read, write)
    assert committed["status"] == "committed", "fixture event was not committed"
    receipt = service.event_receipt(scope, "wb", binding, "event")
    assert receipt is not None, "committed event has no receipt"
    assert receipt["status"] == "committed"
    assert receipt["refs"], "committed receipt must keep its references"
    assert set(receipt) <= {"status", "operation_id", "refs", "retry_action"}
    assert "content" not in receipt and "build_plan" not in receipt


def test_partial_receipt_requests_original_write_retry(setup):
    service, scope, write, read, _, units = setup
    event, binding = _bound_event(service, scope, write, read)
    unit = write("saved body", {"continuity_event": "event"})[0]
    key = service.event_key("wb", binding, "event")
    service.repository.save(
        scope,
        "events",
        key,
        {
            "digest": "d",
            "status": "partial",
            "refs": [asdict(MemoryRef.of(unit))],
            "connector": "wb",
            "task_id": event.task_id,
        },
    )
    receipt = service.event_receipt(scope, "wb", binding, "event")
    assert receipt["status"] == "partial"
    assert receipt["retry_action"] == "retry_original_write"
    assert "content" not in receipt


def test_unknown_receipt_requests_recovery_then_retry(setup):
    service, scope, write, read, _, _ = setup
    event, binding = _bound_event(service, scope, write, read)
    key = service.event_key("wb", binding, "event")
    service.repository.save(
        scope,
        "events",
        key,
        {
            "digest": "d",
            "status": "unknown",
            "refs": [],
            "connector": "wb",
            "task_id": event.task_id,
        },
    )
    receipt = service.event_receipt(scope, "wb", binding, "event")
    assert receipt["status"] == "unknown"
    assert receipt["retry_action"] == "recover_then_retry"
    assert receipt["refs"] == []


def test_never_executed_event_returns_none(setup):
    service, scope, _, read, _, _ = setup
    assert service.event_receipt(scope, "wb", "missing-binding", "missing-event") is None


def test_probe_changes_no_registry_record(setup):
    service, scope, write, read, _, _ = setup
    event, binding = _bound_event(service, scope, write, read)
    service.record(scope, event, "wb", read, write)
    before = _snapshot(service, scope)
    service.event_receipt(scope, "wb", binding, "event")
    assert _snapshot(service, scope) == before, "read-only probe modified the registry"
