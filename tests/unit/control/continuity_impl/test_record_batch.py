"""Five-phase reliable batch write: bodies in one call, background/revision per task."""

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    MemoryRef,
    TaskRequest,
)
from jiuwen_memory.control.continuity_impl.service import ContinuityService
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="project")


def _service(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    service = ContinuityService(
        __import__(
            "jiuwen_memory.control.continuity_impl.repository", fromlist=["ContinuityRepository"]
        ).ContinuityRepository(kv, single_writer=True, allow_ephemeral=True),
        mode="continuity",
    )
    return service, kv


def _read(_ref):
    return None


def _bind(service):
    task = service.task(_SCOPE, TaskRequest("create", "create"), "owner", _read)
    bound = service.task(
        _SCOPE,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="c"),
        "owner",
        _read,
    )
    return task["task_id"], bound["task"]["bindings"][0]["binding_id"]


def test_record_batch_writes_bodies_once_and_bumps_task_once(tmp_path):
    service, kv = _service(tmp_path)
    try:
        task_id, binding = _bind(service)
        events = [
            ContinuityEvent(task_id, binding, f"e{index}", f"body {index}") for index in range(3)
        ]
        calls = []

        def write_batch(pairs):
            calls.append(len(pairs))
            return [
                [MemoryUnit(id=f"u-{i}", scope=_SCOPE, segments=[Segment(content=content)])]
                for i, (content, _meta) in enumerate(pairs)
            ]

        receipts = service.record_batch(
            _SCOPE, events, ["wb"] * 3, _read, write_batch,
        )
        assert calls == [3], "all bodies must go out in one batched call"
        assert [receipt["status"] for receipt in receipts] == ["committed"] * 3
        task = service.repository.get(_SCOPE, "tasks", task_id)
        # create → 0, bind → 2, and the whole 3-event batch bumps exactly once → 3.
        assert task["revision"] == 3
        assert len(task["background"]) == 3
    finally:
        service.close()
        kv.close()


def test_record_batch_replays_without_rewriting_bodies(tmp_path):
    service, kv = _service(tmp_path)
    try:
        task_id, binding = _bind(service)
        event = ContinuityEvent(task_id, binding, "e1", "body")

        def write_batch(pairs):
            return [
                [MemoryUnit(id=f"u-{i}", scope=_SCOPE, segments=[Segment(content=c)])]
                for i, (c, _m) in enumerate(pairs)
            ]

        first = service.record_batch(_SCOPE, [event], ["wb"], _read, write_batch)
        calls = []

        def replay_batch(pairs):
            calls.append(pairs)
            return []

        second = service.record_batch(_SCOPE, [event], ["wb"], _read, replay_batch)
        assert calls == [], "a committed receipt must not rewrite the body"
        assert second[0]["status"] == "committed"
        assert second[0]["refs"] == first[0]["refs"]
    finally:
        service.close()
        kv.close()


def test_crash_after_body_write_leaves_recoverable_unknown_receipts(tmp_path):
    """Receipts must be durable *before* the body, or a crash silently duplicates writes."""
    service, kv = _service(tmp_path)
    try:
        task_id, binding = _bind(service)
        event = ContinuityEvent(task_id, binding, "e1", "body")
        store: dict = {}

        def read(ref):
            return store.get(ref.unit_id)

        written = []

        def crashing_write(pairs):
            for index, (content, metadata) in enumerate(pairs):
                unit = MemoryUnit(
                    id=f"u{index}",
                    scope=_SCOPE,
                    segments=[Segment(content=content)],
                    system_metadata=dict(metadata),
                )
                store[unit.id] = unit
                written.append(unit)
            raise RuntimeError("crash after the body commit")

        receipts = service.record_batch(_SCOPE, [event], ["wb"], read, crashing_write)
        assert receipts[0]["status"] == "unknown"

        # Replaying must not write the body a second time: the durable receipt short-circuits.
        replay_calls = []
        replay = service.record_batch(
            _SCOPE, [event], ["wb"], read, lambda pairs: replay_calls.append(pairs) or []
        )
        assert replay_calls == [], "a durable unknown receipt must not re-write the body"
        assert replay[0]["status"] == "unknown"
        assert replay[0]["operation_id"] == receipts[0]["operation_id"]

        # The saved refs are what recovery reconciles against.
        recovered = service.recover(
            _SCOPE,
            ContinuityRecovery(
                "event", receipts[0]["operation_id"], [MemoryRef.of(unit) for unit in written]
            ),
            read,
        )
        # Recovery attaches the real refs (partial); the next write promotes it.
        assert recovered["status"] == "partial"
        final_calls = []
        final = service.record_batch(
            _SCOPE, [event], ["wb"], read, lambda pairs: final_calls.append(pairs) or []
        )
        assert final_calls == []
        assert final[0]["status"] == "committed"
    finally:
        service.close()
        kv.close()


def test_batch_maps_partial_body_results_per_item(tmp_path):
    """A single item without a body stays unknown instead of being marked committed."""
    service, kv = _service(tmp_path)
    try:
        task_id, binding = _bind(service)
        events = [
            ContinuityEvent(task_id, binding, f"e{index}", f"body {index}")
            for index in range(2)
        ]

        def half_write(pairs):
            return [
                [
                    MemoryUnit(
                        id="only-first",
                        scope=_SCOPE,
                        segments=[Segment(content=pairs[0][0])],
                        system_metadata=dict(pairs[0][1]),
                    )
                ],
                [],
            ]

        receipts = service.record_batch(_SCOPE, events, ["wb"] * 2, _read, half_write)
        assert [receipt["status"] for receipt in receipts] == ["committed", "unknown"]
    finally:
        service.close()
        kv.close()
