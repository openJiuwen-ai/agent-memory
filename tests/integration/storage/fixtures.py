"""Local-database helpers for storage maintenance integration tests."""

import sqlite3
from contextlib import closing

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.redis_space_cache import RedisSpaceCache
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore


def seed_plaintext_memories(
    tmp_path, count: int, scope: Scope, content: str = "secret memory"
) -> int:
    store = SQLiteKVStore(str(tmp_path / "memory.db"))
    try:
        store.health()
    finally:
        store.close()
    rows = []
    for index in range(count):
        unit = MemoryUnit(id=f"batch-{index:05d}", scope=scope, segments=[Segment(content=content)])
        rows.append((scope.org, scope.space, scope.user, scope.agent, scope.session,
                     memory_key(unit.id), dumps(unit)))
    with closing(sqlite3.connect(tmp_path / "memory.db")) as connection:
        connection.executemany(
            'INSERT INTO kv (org,space,"user",agent,session,key,value,expires_at) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", rows,
        )
        connection.commit()
    return len(rows[0][-1])


class ControlledRedisStore(RedisKVStore):
    """Expose this test instance's background cache to the fixture."""

    @property
    def cache(self):
        return self._space_cache


class ControlledSpaceCache(RedisSpaceCache):
    """Deterministic lifecycle, deadline and scan controls for Redis integration tests."""

    def expire_sample(self):
        self._next_daily = 0

    @property
    def scan_pages(self):
        return self._pages

    @property
    def stopped(self):
        return self._worker is None and self._users == 0

    @property
    def users(self):
        return self._users

    def wait_for_shutdown(self):
        self._stop.wait()
