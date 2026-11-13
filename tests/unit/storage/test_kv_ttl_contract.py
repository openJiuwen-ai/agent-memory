# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``KVStore.remaining_ttl`` 契约：永久记录 0.0；缺失 / 已过期 / 不可知一律 None。

迁移与重新分区按 ``None`` 拒绝搬迁。把「已过期但尚未物理删除」当成 0.0 会让一条本该
消失的记录变成永久副本，因此契约必须在每个后端统一。
"""

from __future__ import annotations

import math
import sqlite3
import time

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.postgres_kv import PostgresKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")
KEY = memory_key("u1")


def _payload(uid: str = "u1") -> bytes:
    from jiuwen_memory.common.type_def import MemoryTier, MemoryUnit, Segment

    return dumps(
        MemoryUnit(
            id=uid, scope=SCOPE, tier=MemoryTier.CORE, segments=[Segment(content=uid)]
        )
    )


def _expire_sqlite(db_path: str, key: str) -> None:
    """把 SQLite 行的到期时间改到过去（保留记录，模拟"已过期未物理删除"）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('UPDATE kv SET expires_at = ? WHERE key = ?', (time.time() - 1, key))
        conn.commit()
    finally:
        conn.close()


def test_in_memory_remaining_ttl_contract() -> None:
    store = InMemoryKVStore()
    assert store.remaining_ttl(SCOPE, KEY) is None  # 缺失

    store.insert(SCOPE, KEY, _payload())
    assert math.isclose(store.remaining_ttl(SCOPE, KEY), 0.0)  # 永久

    store.update(SCOPE, KEY, _payload(), ttl=60.0)
    remaining = store.remaining_ttl(SCOPE, KEY)
    assert remaining is not None and 0 < remaining <= 60.0

    store.insert(SCOPE, memory_key("u2"), _payload("u2"), ttl=0.01)
    time.sleep(0.02)
    # 已过期但记录仍在内存里：按缺失处理，不能返回 0.0。
    assert store.remaining_ttl(SCOPE, memory_key("u2")) is None


def test_sqlite_remaining_ttl_contract(tmp_path) -> None:
    db_path = str(tmp_path / "ttl.db")
    store = SQLiteKVStore(db_path=db_path)
    assert store.remaining_ttl(SCOPE, KEY) is None

    store.insert(SCOPE, KEY, _payload())
    assert math.isclose(store.remaining_ttl(SCOPE, KEY), 0.0)

    store.update(SCOPE, KEY, _payload(), ttl=60.0)
    remaining = store.remaining_ttl(SCOPE, KEY)
    assert remaining is not None and 0 < remaining <= 60.0

    _expire_sqlite(db_path, KEY)
    assert store.remaining_ttl(SCOPE, KEY) is None


class _FakeRedisClient:
    def __init__(self, pttl_millis: int) -> None:
        self._pttl = pttl_millis

    def pttl(self, name: str) -> int:
        return self._pttl


class _FakeClientRedis(RedisKVStore):
    def __init__(self, pttl_millis: int) -> None:
        super().__init__(url="redis://example.invalid:6379/0")
        self._fake = _FakeRedisClient(pttl_millis)

    @property
    def client(self):
        return self._fake


@pytest.mark.parametrize(
    ("pttl_millis", "expected"),
    [
        (-2, None),  # key 不存在
        (-1, 0.0),  # 永久记录
        (0, None),  # 剩余不足 1ms：等价于已过期
        (5000, 5.0),
    ],
)
def test_redis_remaining_ttl_contract(pttl_millis: int, expected: float | None) -> None:
    assert _FakeClientRedis(pttl_millis).remaining_ttl(SCOPE, KEY) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, None),  # 行不存在
        ((None, None), 0.0),  # expires_at IS NULL：永久
        ((1_700_000_000.0, -0.5), None),  # 已过期
        ((1_700_000_000.0, 30.0), 30.0),
    ],
)
def test_postgres_remaining_ttl_contract(monkeypatch, row, expected) -> None:
    store = PostgresKVStore(dsn="postgresql://user:secret@localhost:5432/db")
    monkeypatch.setattr(store, "_fetch_one", lambda *args, **kwargs: row, raising=False)
    assert store.remaining_ttl(SCOPE, KEY) == expected
