"""Pending turns and task selection are keyed by connector + project + session + turn."""

from __future__ import annotations

import pytest

from jiuwen_memory_entry.local.turns import Selection, TurnKey, TurnStore

pytestmark = pytest.mark.unit

KEY = TurnKey(connector="c1", project="p1", host_session_id="s1", turn_id="t1")


def test_turn_store_is_owner_only(tmp_path):
    path = tmp_path / "turns.db"
    TurnStore(path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"turns hold user text but are readable: {oct(mode)}"


def test_pending_is_created_once_and_returned(tmp_path):
    store = TurnStore(tmp_path / "turns.db")
    assert store.put_pending(KEY, "task-a", "binding-a", "first") is True
    assert store.put_pending(KEY, "task-b", "binding-b", "second") is False
    pending = store.get_pending(KEY)
    assert pending.task_id == "task-a" and pending.user_text == "first"


def test_pending_expires(tmp_path):
    now = [0.0]
    store = TurnStore(tmp_path / "turns.db", clock=lambda: now[0])
    store.put_pending(KEY, "task-a", "binding-a", "text", ttl=10)
    now[0] = 11.0
    assert store.get_pending(KEY) is None
    assert store.put_pending(KEY, "task-b", "binding-b", "new") is True


def test_projects_and_connectors_do_not_interfere(tmp_path):
    store = TurnStore(tmp_path / "turns.db")
    other_project = TurnKey("c1", "p2", "s1", "t1")
    other_connector = TurnKey("c2", "p1", "s1", "t1")
    store.put_pending(KEY, "task-a", "binding-a", "a")
    store.put_pending(other_project, "task-p2", "binding-p2", "b")
    store.put_pending(other_connector, "task-c2", "binding-c2", "c")
    assert store.get_pending(KEY).task_id == "task-a"
    assert store.get_pending(other_project).task_id == "task-p2"
    assert store.get_pending(other_connector).task_id == "task-c2"


def test_selection_roundtrip_overwrites(tmp_path):
    store = TurnStore(tmp_path / "turns.db")
    assert store.get_selection("c1", "p1", "s1") is None
    store.set_selection(Selection("c1", "p1", "s1", "task-a", "binding-a"))
    store.set_selection(Selection("c1", "p1", "s1", "task-b", "binding-b"))
    saved = store.get_selection("c1", "p1", "s1")
    assert (saved.task_id, saved.binding_id) == ("task-b", "binding-b")
