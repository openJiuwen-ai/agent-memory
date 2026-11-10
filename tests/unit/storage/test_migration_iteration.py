"""Maintenance iteration is bounded independently of ordinary whole-Scope scan."""

import json
import tracemalloc

import pytest

from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_keyset_resume_survives_deletions_and_skips_foreign_data(tmp_path, backend, monkeypatch):
    store = InMemoryKVStore() if backend == "memory" else SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        for index in reversed(range(135)):
            store.insert(SCOPE, memory_key(f"{index:04d}"), b"value")
        store.insert(Scope(org="acme", user="bob"), memory_key("foreign"), b"foreign")
        store.insert(SCOPE, "/messages/not-memory", b"raw")
        store.insert(SCOPE, memory_key("expired"), b"expired", ttl=-1)
        monkeypatch.setattr(store, "scan", lambda *args: pytest.fail("whole scan is forbidden"))
        records = store.migration_records(SCOPE)
        first = next(records)
        records.close()
        store.delete(SCOPE, first.key)
        remaining = list(store.migration_records(SCOPE, after=first.cursor))
        assert len(remaining) == 134 and len({record.key for record in remaining}) == 134
        assert all(record.value == b"value" for record in remaining)
        assert first.key not in {record.key for record in remaining}
    finally:
        if backend == "sqlite":
            store.close()


def test_sqlite_oversized_blob_is_not_materialized(tmp_path):
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        store.insert(SCOPE, memory_key("large"), b"x" * (16 * 1024 * 1024))
        tracemalloc.start()
        try:
            record = next(store.migration_records(SCOPE, max_value_bytes=1024))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert record.value is None and record.cursor
        assert peak < 1024 * 1024, "the oversized SQLite BLOB must not be loaded before rejection"
    finally:
        store.close()


@pytest.mark.parametrize("expired", [False, True])
def test_sqlite_insert_checks_existence_without_loading_the_old_blob(tmp_path, expired):
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        store.insert(SCOPE, "key", b"x" * (16 * 1024 * 1024), ttl=-1 if expired else 0)
        tracemalloc.start()
        try:
            if expired:
                store.insert(SCOPE, "key", b"new")
            else:
                with pytest.raises(ConflictError):
                    store.insert(SCOPE, "key", b"new")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 1024 * 1024, "existence checks must not materialize the old BLOB"
    finally:
        store.close()


def test_redis_resume_preserves_unvisited_scan_keys_after_clear(monkeypatch):
    scope = Scope(org="o", space="s", user="u", agent="a", session="z")
    prefix = "o:s:u:a:z:"
    keys = [prefix + memory_key(str(index)) for index in range(5)]
    values = {key: b"value" for key in keys}
    values[keys[2]] = b"oversized"

    class RedisClient:
        @staticmethod
        def scan(cursor, *, match, count):
            assert count == 64
            assert match == "o:s:u:a:z:/memory/*"
            return (1, keys[:3]) if cursor == 0 else (0, keys[3:])

        @staticmethod
        def eval(script, count, key, maximum):
            assert "STRLEN" in script and count == 1
            value = values.get(key)
            return (None, 1) if value is not None and len(value) > maximum else (value, 0)

    monkeypatch.setattr(RedisKVStore, "client", property(lambda self: RedisClient()))
    store = RedisKVStore()
    records = store.migration_records(scope, max_value_bytes=5)
    first = next(records)
    records.close()
    del values[keys[0]]
    remaining = list(store.migration_records(scope, after=first.cursor, max_value_bytes=5))
    assert [record.key for record in remaining] == [memory_key(str(index)) for index in range(1, 5)]
    assert remaining[1].value is None
    with pytest.raises(ValidationError):
        list(store.migration_records(scope, after=json.dumps({"scan": 0, "pending": ["foreign"]})))
