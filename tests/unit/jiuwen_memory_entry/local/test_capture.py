"""Capture: freeze a turn, write once, replay by status (F17 D8)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.api import MemoryUnit, Scope, Segment
from jiuwen_memory.common.errors import WriteIncompleteError
from jiuwen_memory_entry.local.hooks import (
    CaptureRequest,
    HookService,
    InstallPolicy,
)
from jiuwen_memory_entry.local.turns import TurnKey, TurnStore

pytestmark = pytest.mark.unit

SCOPE = Scope(org="local", space="p1")
KEY = TurnKey("codex-1", "p1", "s1", "t1")


class FakeApi:
    def __init__(self):
        self.adds = []
        self.state = "open"
        self.fail = None
        self.deny_get = False
        self.receipt = None
        self.recovered = []

    def get(self, unit_id, scope, *, security, as_of=None):
        if self.deny_get:
            from jiuwen_memory.common.errors import PermissionDeniedError

            raise PermissionDeniedError("get")
        return None

    def continuity_task(self, scope, request, *, security):
        return {
            "state": self.state,
            "bindings": [
                {"binding_id": "b1", "active": True, "connector_id": "codex-1",
                 "context_id": "s1"}
            ],
        }

    def continuity_recover(self, scope, *, security, recovery=None):
        self.recovered.append(recovery)
        return {"status": "recovered"}

    def continuity_receipt_status(self, scope, task_id, binding_id, event_id):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt

    def add(self, content, scope, *, security, continuity):
        self.adds.append((content, continuity))
        if self.fail is not None:
            raise self.fail
        unit = MemoryUnit(
            id=f"u{len(self.adds)}", scope=scope, segments=[Segment(content=content)]
        )
        return [unit]


def _service(tmp_path, api):
    store = TurnStore(tmp_path / "turns.db")
    policy = InstallPolicy(
        install_id="codex-1",
        connector_id="codex-1",
        capture="task",
        fixed_project="p1",
        projects=("p1",),
        org="local",
        actor=Scope(org="local", agent="codex-1"),
    )
    return HookService(api, store, policy), store


def _pending(store, *, injected=("INJECTED",)):
    store.put_pending(KEY, "task-a", "b1", "user question", injected=injected)


def _capture(service, *, answer="the answer", prompt="user question"):
    return service.capture(
        None,
        CaptureRequest("s1", "t1", prompt, answer),
    )


def test_capture_writes_once_and_replays(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    first = _capture(service)
    assert first.status == "committed", first.reason
    assert len(api.adds) == 1
    assert "INJECTED" not in api.adds[0][0]
    second = _capture(service)
    assert second.status == "committed"
    assert len(api.adds) == 1, "replaying a committed turn must not write again"


def test_same_turn_with_different_content_conflicts(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    assert _capture(service).status == "committed"
    conflict = _capture(service, answer="a different answer")
    assert conflict.status == "conflict"
    assert len(api.adds) == 1


def test_unbound_turn_is_not_written(tmp_path):
    api = FakeApi()
    service, _ = _service(tmp_path, api)
    result = _capture(service)
    assert result.status == "not_written"
    assert api.adds == []


def test_revoked_binding_is_rejected_before_writing(tmp_path):
    api = FakeApi()
    api.state = "closed"
    service, store = _service(tmp_path, api)
    _pending(store)
    result = _capture(service)
    assert result.status == "rejected"
    assert api.adds == []


def test_partial_write_is_retried_with_the_original_request(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    api.fail = WriteIncompleteError(
        operation_id="op-key", status="partial", retry_action="retry_original_write"
    )
    first = _capture(service)
    assert first.status == "partial"
    api.fail = None
    second = _capture(service)
    assert second.status == "committed", second.reason
    assert len(api.adds) == 2
    assert api.adds[0][1].event_id == api.adds[1][1].event_id


def test_operation_store_failure_is_unconfirmed_not_not_written(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)

    def locked(self, operation):
        raise sqlite3.OperationalError("database is locked")

    store.create_operation = locked.__get__(store, TurnStore)
    result = _capture(service)
    assert result.status == "unconfirmed"
    assert api.adds == []


def test_concurrent_captures_write_once(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _capture(service), range(2)))
    assert [item.status for item in results] == ["committed", "committed"]
    assert len(api.adds) == 1


def test_capture_survives_hook_restart(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    service.capture(None, CaptureRequest("s1", "t1", "user question", "the answer"))
    restarted, _ = _service(tmp_path, api)
    replay = restarted.capture(None, CaptureRequest("s1", "t1", "user question", "the answer"))
    assert replay.status == "committed"
    assert len(api.adds) == 1


def test_committed_replay_hides_refs_without_read_permission(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    first = _capture(service)
    assert first.refs
    api.deny_get = True
    replay = _capture(service)
    assert replay.status == "committed"
    assert replay.refs == [] and replay.operation_id == ""


def test_retry_advances_the_frozen_operation(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    api.fail = WriteIncompleteError(
        operation_id="op-key", status="unknown", retry_action="recover_then_retry"
    )
    first = _capture(service)
    assert first.status == "unknown"
    api.fail = None
    retried = service.retry(None, first.local_op_id)
    assert retried.status == "committed", retried.reason


def test_retry_refuses_another_installs_operation(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    first = _capture(service)
    other, _ = _service(tmp_path, FakeApi())
    other._policy = InstallPolicy(
        install_id="codex-2",
        connector_id="codex-2",
        capture="task",
        fixed_project="p1",
        projects=("p1",),
        org="local",
    )
    from jiuwen_memory.common.errors import ValidationError

    with pytest.raises(ValidationError):
        other.retry(None, first.local_op_id)


def _frozen_partial(tmp_path, api):
    service, store = _service(tmp_path, api)
    _pending(store)
    api.fail = WriteIncompleteError(
        operation_id="kernel-key", status="partial", retry_action="retry_original_write"
    )
    result = _capture(service)
    assert result.status == "partial"
    return service, result.local_op_id


def test_retry_uses_committed_receipt_without_writing(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    before = len(api.adds)
    api.receipt = SimpleNamespace(
        status="committed", operation_id="kernel-key",
        refs=[{"unit_id": "u1", "scope": {"org": "local", "space": "p1"}}], retry_action="",
    )
    result = service.retry(None, op_id)
    assert result.status == "committed"
    assert len(api.adds) == before, "a committed receipt must not be written again"
    assert result.refs and result.operation_id == "kernel-key"


def test_retry_reports_a_non_terminal_receipt_without_writing(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    before = len(api.adds)
    api.receipt = SimpleNamespace(status="unknown", operation_id="kernel-key", refs=[],
                                  retry_action="")
    result = service.retry(None, op_id)
    assert result.status == "unknown"
    assert len(api.adds) == before
    assert "no retry action" in result.reason


def test_retry_fails_closed_when_the_receipt_probe_errors(tmp_path):
    from jiuwen_memory.common.errors import ValidationError

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    before = len(api.adds)
    api.receipt = RuntimeError("boom")
    with pytest.raises(ValidationError):
        service.retry(None, op_id)
    assert len(api.adds) == before, "a failed probe must not fall through to a write"


def test_operation_lock_is_shared_across_hook_services(tmp_path):
    api = FakeApi()
    first, store = _service(tmp_path, api)
    second, _ = _service(tmp_path, api)
    _pending(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda service: _capture(service), (first, second))
        )
    assert [item.status for item in results] == ["committed", "committed"]
    assert len(api.adds) == 1, "two HookService instances wrote the same turn twice"


def test_capture_replay_consults_the_receipt_before_writing(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    before = len(api.adds)
    api.receipt = SimpleNamespace(
        status="committed", operation_id="kernel-key",
        refs=[{"unit_id": "u1", "scope": {"org": "local", "space": "p1"}}], retry_action="",
    )
    result = _capture(service)
    assert result.status == "committed"
    assert len(api.adds) == before, "replay must not write after a committed receipt"


def test_capture_replay_fails_closed_when_the_receipt_probe_errors(tmp_path):
    api = FakeApi()
    service, _op_id = _frozen_partial(tmp_path, api)
    before = len(api.adds)
    api.receipt = RuntimeError("boom")
    result = _capture(service)
    assert result.status == "unconfirmed"
    assert len(api.adds) == before


def test_terminal_operation_releases_its_lock(tmp_path):
    from jiuwen_memory_entry.local import hooks

    api = FakeApi()
    service, store = _service(tmp_path, api)
    _pending(store)
    result = _capture(service)
    assert result.status == "committed"
    assert result.local_op_id not in hooks._OPERATION_LOCKS, (
        "terminal operations must not accumulate locks"
    )


def test_non_terminal_operation_keeps_its_lock(tmp_path):
    from jiuwen_memory_entry.local import hooks

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    assert op_id in hooks._OPERATION_LOCKS, "a retryable operation must stay serialized"


def test_terminal_receipt_releases_the_lock(tmp_path):
    from types import SimpleNamespace

    from jiuwen_memory_entry.local import hooks

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    api.receipt = SimpleNamespace(status="expired", operation_id="k", refs=[],
                                  retry_action="")
    result = service.retry(None, op_id)
    assert result.status == "expired"
    assert op_id not in hooks._OPERATION_LOCKS


def test_non_terminal_receipt_hides_refs_without_read_permission(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    api.deny_get = True
    api.receipt = SimpleNamespace(
        status="partial", operation_id="kernel-key",
        refs=[{"unit_id": "u1", "scope": {"org": "local", "space": "p1"}}],
        retry_action="",
    )
    result = service.retry(None, op_id)
    assert result.status == "partial"
    assert result.refs == [], "refs must not be echoed after read permission is lost"
    assert result.operation_id == "", "operation_id must not be echoed either"


def test_non_terminal_receipt_echoes_refs_with_read_permission(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    refs = [{"unit_id": "u1", "scope": {"org": "local", "space": "p1"}}]
    api.receipt = SimpleNamespace(status="partial", operation_id="kernel-key", refs=refs,
                                  retry_action="")
    result = service.retry(None, op_id)
    assert result.refs == refs and result.operation_id == "kernel-key"


def test_stopped_record_releases_the_lock(tmp_path):
    from jiuwen_memory_entry.local import hooks

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    # A stopped record whose request was already cleaned up: advancing cannot write and
    # returns a non-terminal CaptureResult, but the record itself is terminal.
    service._turns.update_operation(op_id, status="stopped", request=None)
    before = len(api.adds)
    hooks._operation_lock(op_id)
    service.retry(None, op_id)
    assert len(api.adds) == before
    assert op_id not in hooks._OPERATION_LOCKS, (
        "a stopped record is terminal for locking even though no CaptureResult says so"
    )


def test_stop_without_prompt_uses_the_recall_user_text(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    store.put_pending(KEY, "task-a", "b1", "the original question", injected=())
    result = service.capture(None, CaptureRequest("s1", "t1", "", "the answer"))
    assert result.status == "committed", result.reason
    assert "user: the original question" in api.adds[0][0]


def test_expired_pending_does_not_break_replay(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    store.put_pending(KEY, "task-a", "b1", "user question", injected=())
    api.fail = WriteIncompleteError(
        operation_id="k", status="partial", retry_action="retry_original_write"
    )
    first = service.capture(None, CaptureRequest("s1", "t1", "user question", "answer"))
    assert first.status == "partial"
    store.delete_pending(KEY)
    api.fail = None
    replay = service.capture(None, CaptureRequest("s1", "t1", "user question", "answer"))
    assert replay.status == "committed", replay.reason


def test_purge_expired_removes_the_rows(tmp_path):
    now = [100.0]
    store = TurnStore(tmp_path / "turns.db", clock=lambda: now[0])
    store.put_pending(KEY, "t", "b", "secret text", ttl=10)
    now[0] = 200.0
    assert store.purge_expired() == 1
    rows = store._conn.execute("SELECT COUNT(*) FROM pending_turn").fetchone()[0]
    assert rows == 0


def test_committed_operation_id_is_stable(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    api.receipt = SimpleNamespace(
        status="committed", operation_id="kernel-key", refs=[], retry_action=""
    )
    service, store = _service(tmp_path, api)
    _pending(store)
    first = _capture(service)
    second = _capture(service)
    assert first.operation_id == second.operation_id == "kernel-key"
    stored = store.get_operation_by_id(first.local_op_id)
    assert stored.event_id == first.event_id and stored.event_id != "kernel-key"
    assert stored.receipt["operation_id"] == "kernel-key"
    assert stored.receipt["event_id"] == stored.event_id


def test_partial_retry_action_replays_the_original_write(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    api.fail = None
    api.receipt = SimpleNamespace(
        status="partial", operation_id="kernel-key", refs=[],
        retry_action="retry_original_write",
    )
    before = len(api.adds)
    result = service.retry(None, op_id)
    assert result.status == "committed", result.reason
    assert len(api.adds) == before + 1


def test_recover_then_retry_runs_recover_before_writing(tmp_path):
    from types import SimpleNamespace

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    api.fail = None
    api.receipt = SimpleNamespace(
        status="unknown", operation_id="kernel-key", refs=[],
        retry_action="recover_then_retry",
    )
    result = service.retry(None, op_id)
    assert result.status == "committed", result.reason


def test_permission_loss_stops_the_operation(tmp_path):
    from types import SimpleNamespace

    from jiuwen_memory.common.errors import PermissionDeniedError

    api = FakeApi()
    service, op_id = _frozen_partial(tmp_path, api)
    api.receipt = SimpleNamespace(
        status="partial", operation_id="kernel-key", refs=[],
        retry_action="retry_original_write",
    )
    api.fail = PermissionDeniedError("write")
    result = service.retry(None, op_id)
    assert result.status == "stopped"
    assert service._turns.get_operation_by_id(op_id).status == "stopped"


def test_committed_retry_without_pending_still_matches_the_same_answer(tmp_path):
    api = FakeApi()
    service, store = _service(tmp_path, api)
    store.put_pending(KEY, "task-a", "b1", "the original question", injected=())
    first = service.capture(None, CaptureRequest("s1", "t1", "the original question", "answer"))
    assert first.status == "committed"
    store.delete_pending(KEY)
    retry = service.capture(None, CaptureRequest("s1", "t1", "", "answer"))
    assert retry.status == "committed", retry.reason
    different = service.capture(None, CaptureRequest("s1", "t1", "", "another answer"))
    assert different.status == "conflict"
