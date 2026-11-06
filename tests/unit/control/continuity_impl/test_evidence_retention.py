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


def test_without_reader_only_active_tasks_are_checked(setup):
    service, scope, write, read, _, _ = setup
    evidence = MemoryRef.of(write("evidence")[0])
    a = MemoryRef.of(write("a")[0])
    service.relation(
        scope,
        RelationRecord("r", a, evidence, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    # No reader: relation-truth scan is skipped, so the relation reference is not seen.
    assert service.is_referenced(scope, evidence) is False
    assert service.is_referenced(scope, evidence, read=read) is True


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


@pytest.mark.parametrize("failure", ["denied", "malformed", "bug"])
def test_relation_read_failure_does_not_allow_cleanup(setup, failure):
    from jiuwen_memory.common.errors import PermissionDeniedError

    service, scope, write, read, _, _ = setup
    evidence = MemoryRef.of(write("evidence")[0])
    target = MemoryRef.of(write("target")[0])
    service.relation(
        scope, RelationRecord("r", evidence, target, "supports"), 0, read, write,
    )

    def unavailable(ref):
        if failure == "denied":
            raise PermissionDeniedError("get")
        if failure == "malformed":
            raise ValueError("invalid relation payload")
        raise RuntimeError("unexpected programming failure")

    if failure == "bug":
        with pytest.raises(RuntimeError, match="unexpected programming failure"):
            service.is_referenced(scope, evidence, read=unavailable)
    else:
        assert service.is_referenced(scope, evidence, read=unavailable) is True
