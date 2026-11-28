"""Background recovery advances non-terminal operations through the install token."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import threading
import time

import pytest

from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.recovery import OperationRecovery
from jiuwen_memory_entry.local.turns import Operation, TurnKey, TurnStore

pytestmark = pytest.mark.unit


def _operation(store, index: int, status: str) -> Operation:
    key = TurnKey("codex-1", "p1", "s1", f"t{index}")
    return store.create_operation(
        Operation(
            local_op_id=f"op-{index}",
            key=key,
            install_id="codex-1",
            actor={},
            task_id="task",
            binding_id="b1",
            event_id=f"e{index}",
            digest="d",
            status=status,
            request={"content": "x", "project": "p1", "task_id": "task",
                     "binding_id": "b1", "event_id": f"e{index}", "digest": "d"},
            request_expires_at=time.time() + 3600,
        )
    )


class FakeClient:
    posts: list[tuple[str, dict]] = []

    def __init__(self, discovery, token):
        self.token = token

    def post(self, path, payload, **kwargs):
        self.posts.append((path, payload))
        return 200, {}


@pytest.fixture
def home(tmp_path) -> MemoryHome:
    memory_home = MemoryHome(tmp_path / "home")
    memory_home.installs_dir.mkdir(parents=True)
    (memory_home.installs_dir / "codex-1").mkdir()
    (memory_home.installs_dir / "codex-1" / "token").write_text("token\n", encoding="utf-8")
    FakeClient.posts = []
    return memory_home


def test_run_once_advances_only_non_terminal_operations(home, monkeypatch):
    store = TurnStore(home.data_dir / "turns.db")
    prepared = _operation(store, 1, "prepared")
    _operation(store, 2, "committed")
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", FakeClient)
    recovery = OperationRecovery(home, lambda: object(), store)
    assert recovery.run_once() == 1
    assert FakeClient.posts == [("/local/hook/retry", {"local_op_id": prepared.local_op_id})]


def test_operations_without_a_token_are_skipped(home, monkeypatch):
    (home.installs_dir / "codex-1" / "token").unlink()
    store = TurnStore(home.data_dir / "turns.db")
    _operation(store, 1, "prepared")
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", FakeClient)
    assert OperationRecovery(home, lambda: object(), store).run_once() == 0
    assert FakeClient.posts == []


def test_expired_requests_are_discarded_before_recovery(home, monkeypatch):
    store = TurnStore(home.data_dir / "turns.db", clock=lambda: 100.0)
    _operation(store, 1, "prepared")
    store._conn.execute("UPDATE turn_operation SET request_expires_at = 50")
    store._conn.commit()
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", FakeClient)
    OperationRecovery(home, lambda: object(), store).run_once()
    assert store.get_operation_by_id("op-1").request is None


def test_loop_stops_promptly(home, monkeypatch):
    store = TurnStore(home.data_dir / "turns.db")
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", FakeClient)
    recovery = OperationRecovery(home, lambda: None, store, interval=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=recovery.loop, args=(stop,))
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_missing_token_falls_back_to_a_read_only_receipt_probe(home, monkeypatch):
    from types import SimpleNamespace

    (home.installs_dir / "codex-1" / "token").unlink()
    store = TurnStore(home.data_dir / "turns.db")
    operation = _operation(store, 1, "partial")
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", FakeClient)

    def probe(_op):
        return SimpleNamespace(
            status="committed", operation_id="k", refs=[], retry_action=""
        )

    OperationRecovery(home, lambda: object(), store, receipt_probe=probe).run_once()
    assert FakeClient.posts == []
    assert store.get_operation_by_id(operation.local_op_id).status == "committed"


def test_revoked_token_stops_a_partial_operation(home, monkeypatch):
    from types import SimpleNamespace

    class Revoked(FakeClient):
        def post(self, path, payload, **kwargs):
            return 403, {"error": "AuthenticationError"}

    store = TurnStore(home.data_dir / "turns.db")
    operation = _operation(store, 1, "partial")
    monkeypatch.setattr("jiuwen_memory_entry.local.recovery.ServiceClient", Revoked)

    def probe(operation_record):
        return SimpleNamespace(
            status="partial", operation_id="k", refs=[],
            retry_action="retry_original_write",
        )
    OperationRecovery(
        home, lambda: object(), store, receipt_probe=probe
    ).run_once()
    record = store.get_operation_by_id(operation.local_op_id)
    assert record.status == "stopped"
    assert record.receipt["reason"] == "write permission revoked"
