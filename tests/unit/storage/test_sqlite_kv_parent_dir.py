# coding: utf-8
"""A nested SQLite KV db_path must work on first use."""

from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore


def test_nested_db_path_creates_the_parent_directory(tmp_path):
    db_path = tmp_path / "data" / "nested" / "memory.db"
    store = SQLiteKVStore(db_path=str(db_path))

    store.insert(Scope(org="o", user="u"), "k", b"v")

    assert db_path.parent.is_dir()
    assert store.get(Scope(org="o", user="u"), "k") == b"v"


def test_in_memory_database_needs_no_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteKVStore(db_path=":memory:")
    store.insert(Scope(org="o"), "k", b"v")
    assert store.get(Scope(org="o"), "k") == b"v"
