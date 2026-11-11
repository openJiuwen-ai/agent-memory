"""Two real SQLite connections must not overwrite or mutate expired maintenance targets."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.security.cryptography import CryptographyProducer
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")


def test_two_connection_import_has_one_winner_without_overwrite(tmp_path, monkeypatch):
    rendezvous = threading.Barrier(2)
    connect = sqlite3.connect

    class CoordinatedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith(("SELECT value, expires_at", "SELECT CASE WHEN length(value)")) and (
                not self.in_transaction
            ):
                row = cursor.fetchone()
                if row is None:
                    rendezvous.wait(timeout=5)
                return SimpleNamespace(fetchone=lambda: row)
            return cursor

    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: connect(
            *args, **kwargs, factory=CoordinatedConnection
        )
    )
    register_plugins()
    crypto = CryptographyProducer.build(
        "local", {"key_provider": {"target": "local", "params": {"key_hex": "11" * 32}}},
        AssemblyContext(),
    )
    stores = [SQLiteKVStore(str(tmp_path / "memory.db")) for _ in range(2)]
    wrappers = [EncryptedKVStore(store, crypto) for store in stores]
    records = [
        dumps(MemoryUnit(id="id", scope=SCOPE, segments=[Segment(content=str(index))]))
        for index in range(2)
    ]
    try:
        for store in stores:
            store.health()

        def imported(index):
            try:
                wrappers[index].migrate_plaintext(
                    SCOPE, memory_key("id"), records[index], operation="migrate"
                )
                return index
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(imported, range(2)))
        winners = [index for index in outcomes if index is not None]
        assert len(winners) == 1, "concurrent imports must conflict instead of replacing the winner"
        assert wrappers[0].get(SCOPE, memory_key("id")) == records[winners[0]]
        stores[1].insert(SCOPE, "after-conflict", b"transaction rolled back")
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("replacement", [None, b"encrypted"])
@pytest.mark.parametrize("wait_for", ["instance", "database"])
def test_cas_checks_expiry_after_all_lock_waits(tmp_path, monkeypatch, replacement, wait_for):
    now = [1000.0]
    held, release, sampled, started = (threading.Event() for _ in range(4))
    watch = [False]
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if watch[0] and wait_for == "instance" and sql == "SELECT 1":
                held.set()
                assert release.wait(5), "health lock holder was not released"
            return super().execute(sql, parameters)

    def opened(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=ObservedConnection)

        def traced(statement):
            if watch[0] and wait_for == "database" and statement.startswith(
                ("BEGIN IMMEDIATE", "UPDATE kv", "DELETE FROM kv")
            ):
                held.set()

        connection.set_trace_callback(traced)
        return connection

    def clock():
        if threading.current_thread().name.startswith("cas"):
            sampled.set()
        return now[0]

    monkeypatch.setattr(sqlite3, "connect", opened)
    monkeypatch.setattr("jiuwen_memory.storage.kv_impl.sqlite_kv_store.time.time", clock)
    path = tmp_path / "memory.db"
    store = SQLiteKVStore(str(path))
    blocker = connect(path, isolation_level=None)
    try:
        store.insert(SCOPE, "key", b"old", ttl=1)
        watch[0] = True
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="health") as health_pool:
            health = None
            if wait_for == "instance":
                health = health_pool.submit(store.health)
                assert held.wait(5)
            else:
                blocker.execute("BEGIN IMMEDIATE")

            def changed():
                started.set()
                return store.compare_exchange(SCOPE, "key", b"old", replacement)

            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cas") as cas_pool:
                outcome = cas_pool.submit(changed)
                assert started.wait(5)
                if wait_for == "database":
                    assert held.wait(5)
                else:
                    # Old code samples before waiting for the instance lock; new code cannot.
                    sampled.wait(0.1)
                now[0] = 1002.0
                release.set()
                blocker.commit()
                assert outcome.result(timeout=5) is False
            if health is not None:
                health.result(timeout=5)
        with closing(connect(path)) as connection:
            row = connection.execute("SELECT value, expires_at FROM kv WHERE key='key'").fetchone()
        assert row == (b"old", 1001.0), "expired records must not be replaced or cleared"
    finally:
        release.set()
        blocker.close()
        store.close()
