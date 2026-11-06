"""Ordered source ingestion through record: durable pending boundary and monotonic cursor."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    TaskRequest,
)
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from tests.unit.control.continuity_impl.test_latest_design import _task

pytestmark = pytest.mark.unit


def test_ordered_record_replay_and_source_conflict(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    event = ContinuityEvent(task, binding, "one", "first", source_position=10, previous_position=0)
    first = service.record(scope, event, "wb", read, write)
    assert first["source_cursor"]["position"] == 10
    second = replace(event, event_id="two", content="second", source_position=20,
                     previous_position=10)
    assert service.record(scope, second, "wb", read, write)["source_cursor"]["position"] == 20
    assert service.record(scope, event, "wb", read, write)["source_cursor"]["position"] == 20
    assert len(units) == 2
    with pytest.raises(ConflictError):
        service.record(scope, replace(event, content="changed"), "wb", read, write)
    with pytest.raises(ConflictError):
        service.record(scope, replace(event, event_id="late"), "wb", read, write)
    assert len(units) == 2


def test_unknown_boundary_survives_service_restart_and_recovery(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    first = ContinuityEvent(task, binding, "one", "first", source_position=1, previous_position=0)

    def lost_ack(content, metadata):
        write(content, metadata)
        raise OSError("lost confirmation")

    unknown = service.record(scope, first, "wb", read, lost_ack)
    assert unknown["status"] == "unknown"
    # Recreate the control repository to ensure no cursor state lives only in its lock.
    kv = service.repository.kv
    service.close()
    service.repository = ContinuityRepository(kv, single_writer=True)
    second = replace(first, event_id="two", source_position=2, previous_position=1)
    with pytest.raises(ConflictError, match="pending"):
        service.record(scope, second, "wb", read, write)
    source = MemoryRef.of(next(iter(units.values())))
    service.recover(scope, ContinuityRecovery("event", unknown["operation_id"], [source]), read)
    assert service.record(scope, first, "wb", read, write)["source_cursor"]["position"] == 1
    assert service.record(scope, second, "wb", read, write)["source_cursor"]["position"] == 2
    assert len(units) == 2
    task_view = service.task(scope, TaskRequest("get", "read", task), "owner", read)
    assert task_view["source_cursors"][binding]["position"] == 2


def test_cutover_records_watermark_and_rejects_new_source_events(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    first = ContinuityEvent(task, binding, "one", "first", source_position=10, previous_position=0)
    service.record(scope, first, "wb", read, write)
    assert service.source_cutover(scope, "wb", "context") is None

    result = service.mark_source_cutover(scope, "wb", "context")
    assert result["cutover_position"] == 10
    assert service.source_cutover(scope, "wb", "context") == 10

    # After cutover the external product must stop emitting source events.
    with pytest.raises(ConflictError, match="cut over"):
        service.record(
            scope,
            replace(first, event_id="after", source_position=20, previous_position=10),
            "wb", read, write,
        )
    assert len(units) == 1


def test_cutover_requires_settled_pending(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    first = ContinuityEvent(task, binding, "one", "first", source_position=1, previous_position=0)

    def lost_ack(content, metadata):
        write(content, metadata)
        raise OSError("lost confirmation")

    service.record(scope, first, "wb", read, lost_ack)
    with pytest.raises(ConflictError, match="pending"):
        service.mark_source_cutover(scope, "wb", "context")
    assert service.source_cutover(scope, "wb", "context") is None


def test_cutover_rejects_unordered_new_events(setup):
    service, scope, write, read, _, units = setup
    task, binding = _task(service, scope, read)
    first = ContinuityEvent(task, binding, "one", "first", source_position=10, previous_position=0)
    service.record(scope, first, "wb", read, write)
    service.mark_source_cutover(scope, "wb", "context")
    # An event without a cursor must not slip past the cutover via the ordered guard.
    unordered = ContinuityEvent(task, binding, "two", "second")
    with pytest.raises(ConflictError, match="cut over"):
        service.record(scope, unordered, "wb", read, write)
    assert len(units) == 1


def test_cutover_is_serialized_with_record(setup):
    import threading
    import time

    service, scope, write, read, _, _ = setup
    task, binding = _task(service, scope, read)
    service.record(
        scope, ContinuityEvent(task, binding, "one", "first", source_position=1,
                               previous_position=0),
        "wb", read, write,
    )
    entered = threading.Event()
    released = threading.Event()

    def cutover():
        entered.set()
        service.mark_source_cutover(scope, "wb", "context")
        released.set()

    # Holding the same lock record() uses must block the cutover, otherwise a concurrent
    # event commit could advance the cursor between the cutover's read and write and be
    # overwritten by a stale watermark.
    with service.repository.lock:
        t = threading.Thread(target=cutover)
        t.start()
        assert entered.wait(timeout=2), "cutover thread never started"
        time.sleep(0.05)
        assert not released.is_set(), "cutover must share the repository critical section"
    t.join()
    assert released.is_set()
    assert service.source_cutover(scope, "wb", "context") == 1
