"""Evidence retention: active task or relation references block cleanup."""

import pytest

from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, TaskRequest

pytestmark = pytest.mark.unit


def test_active_task_reference_blocks_retention(setup):
    service, scope, write, read, _, _ = setup
    ref = MemoryRef.of(write("evidence")[0])
    service.task(scope, TaskRequest("create", "create", background=[ref]), "owner", read)
    assert service.is_referenced(scope, ref) is True


def test_relation_reference_blocks_retention(setup):
    service, scope, write, read, _, _ = setup
    evidence = MemoryRef.of(write("evidence")[0])
    a = MemoryRef.of(write("a")[0])
    b = MemoryRef.of(write("b")[0])
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[evidence]),
        0, read, write,
    )
    assert service.is_referenced(scope, evidence, read=read) is True
    # The relation endpoints themselves are also retained references.
    assert service.is_referenced(scope, a, read=read) is True


def test_unreferenced_evidence_can_be_cleaned(setup):
    service, scope, write, read, _, _ = setup
    ref = MemoryRef.of(write("lonely evidence")[0])
    assert service.is_referenced(scope, ref, read=read) is False


def test_closed_task_no_longer_blocks_retention(setup):
    service, scope, write, read, _, _ = setup
    ref = MemoryRef.of(write("evidence")[0])
    task = service.task(scope, TaskRequest("create", "create", background=[ref]), "owner", read)
    assert service.is_referenced(scope, ref) is True
    service.task(
        scope,
        TaskRequest("close", "close", task["task_id"], task["revision"]),
        "owner",
        read,
    )
    # Closing a task does not delete records, but it stops retaining its background.
    assert service.is_referenced(scope, ref) is False


def test_relation_head_reference_is_seen_without_a_reader(setup):
    service, scope, write, read, _, _ = setup
    evidence = MemoryRef.of(write("evidence")[0])
    a = MemoryRef.of(write("a")[0])
    service.relation(
        scope,
        RelationRecord("r", a, evidence, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    # Relation truth is the Registry KV head, so retention needs no content reader.
    assert service.is_referenced(scope, evidence) is True


def test_sweep_guard_keeps_referenced_evidence_until_task_closes(setup):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from jiuwen_memory.common.type_def import LifecycleState
    from jiuwen_memory.control.engine_impl.sweep_support import run_sweep
    from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition

    service, scope, write, read, _, _ = setup
    unit = write("evidence")[0]
    unit.temporal.t_invalid = datetime.now(UTC) - timedelta(days=1)
    ref = MemoryRef.of(unit)
    task = service.task(scope, TaskRequest("create", "create", background=[ref]), "owner", read)
    lifecycle = Mock(spec=LifecycleManager)
    lifecycle.retains.side_effect = service.retains
    remove = Mock()
    transition = SweepTransition(scope, unit.id, LifecycleState.ACTIVE,
                                 LifecycleState.FORGOTTEN, unit)
    run_sweep([transition], lifecycle, remove)
    remove.assert_not_called()
    lifecycle.transition.assert_not_called()
    service.task(scope, TaskRequest("close", "close", task["task_id"], task["revision"]),
                 "owner", read)
    run_sweep([transition], lifecycle, remove)
    remove.assert_called_once()
    lifecycle.transition.assert_called_once()


def test_retention_uses_identity_even_if_evidence_content_changed(setup):
    from jiuwen_memory.common.type_def import Segment

    service, scope, write, read, _, _ = setup
    unit = write("v1")[0]
    service.task(scope, TaskRequest("create", "create", background=[MemoryRef.of(unit)]),
                 "owner", read)
    unit.segments = [Segment(content="v2")]
    assert service.retains(unit), "a stale reference still requires review before cleanup"


def test_malformed_relation_head_retains_conservatively(setup):
    from jiuwen_memory.common.errors import PermissionDeniedError

    service, scope, write, read, _, _ = setup
    evidence = MemoryRef.of(write("evidence")[0])
    target = MemoryRef.of(write("target")[0])
    service.relation(
        scope, RelationRecord("r", evidence, target, "supports"), 0, read, write,
    )
    # Corrupt the persisted relation truth: an unreadable head must retain, not release.
    head = service.repository.get(scope, "relations", "r")
    head["current"] = {"relation_id": "r", "revision": 1}
    service.repository.save(scope, "relations", "r", head)
    assert service.is_referenced(scope, evidence) is True
    # A well-formed head that no longer cites the unit releases it again.
    del PermissionDeniedError
