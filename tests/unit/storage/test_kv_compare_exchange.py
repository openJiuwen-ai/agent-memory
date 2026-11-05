"""Real backend atomicity, expiry and scope semantics used by maintenance tools."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    backend = (
        InMemoryKVStore() if request.param == "memory" else SQLiteKVStore(str(tmp_path / "kv.db"))
    )
    yield backend
    if isinstance(backend, SQLiteKVStore):
        backend.close()


def test_only_one_competing_change_can_commit(store):
    store.insert(SCOPE, "key", b"old")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda index: store.compare_exchange(SCOPE, "key", b"old", str(index).encode()),
                range(8),
            )
        )
    assert sum(results) == 1
    assert store.get(SCOPE, "key") in {str(index).encode() for index in range(8)}


def test_changed_value_and_foreign_scope_cannot_be_deleted(store):
    store.insert(SCOPE, "key", b"current")
    assert not store.compare_exchange(SCOPE, "key", b"old", None)
    assert not store.compare_exchange(Scope(org="acme", user="bob"), "key", b"current", None)
    assert store.get(SCOPE, "key") == b"current"
    assert store.compare_exchange(SCOPE, "key", b"current", None)
    assert not store.exists(SCOPE, "key")


def test_expired_and_missing_records_are_not_revived(store):
    store.insert(SCOPE, "key", b"expired", ttl=-1)
    assert not store.compare_exchange(SCOPE, "key", b"expired", b"new")
    assert not store.compare_exchange(SCOPE, "missing", b"", b"new")
    assert not store.exists(SCOPE, "key")


def test_sqlite_compare_exchange_preserves_exact_expiry_across_connections(tmp_path):
    path = tmp_path / "kv.db"
    first = SQLiteKVStore(str(path))
    second = SQLiteKVStore(str(path))
    try:
        first.insert(SCOPE, "key", b"old", ttl=120)
        with closing(sqlite3.connect(path)) as connection:
            before = connection.execute("SELECT expires_at FROM kv").fetchone()[0]
        assert second.compare_exchange(SCOPE, "key", b"old", b"encrypted")
        with closing(sqlite3.connect(path)) as connection:
            after = connection.execute("SELECT expires_at FROM kv").fetchone()[0]
        assert before == after and after > time.time()
        assert first.get(SCOPE, "key") == b"encrypted"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize(
    "user,supported",
    [
        ("alice", True),
        ("alice_1", True),
        ("alice:1", False),
        ("alice/1", False),
        ("_", False),
    ],
)
def test_redis_maintenance_refuses_lossy_scope_interpretation_without_connecting(user, supported):
    from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore

    store = RedisKVStore()
    assert store.migration_scope_supported(Scope(org="acme", user=user)) is supported
