# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09：``ConditionalKV`` 契约的真后端集成测试（PG / Redis）。

默认跳过；需要环境：

- PostgreSQL：``AGENT_MEMORY_TEST_PG_DSN``（如 ``postgresql://user:pass@host:5432/db``）
- Redis：``AGENT_MEMORY_TEST_REDIS_PORT``（默认 6300）

两个后端必须各自满足 F09 D6 的原子条件操作与存储侧时间语义。
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv_impl.postgres_kv import PostgresKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore

pytestmark = pytest.mark.integration

PG_DSN = os.getenv("AGENT_MEMORY_TEST_PG_DSN", "")
REDIS_HOST = os.getenv("AGENT_MEMORY_TEST_REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("AGENT_MEMORY_TEST_REDIS_PORT", "6300"))


def _require_postgres() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    if not PG_DSN:
        pytest.skip("AGENT_MEMORY_TEST_PG_DSN is not configured")

    async def probe() -> None:
        conn = await asyncpg.connect(PG_DSN, timeout=3)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    try:
        asyncio.run(probe())
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")


@pytest.fixture(scope="module")
def pg_store():
    _require_postgres()
    table = f"agent_memory_shard_it_{uuid.uuid4().hex[:8]}"
    store = PostgresKVStore(dsn=PG_DSN, table=table, auto_create_schema=True)
    store.health()
    yield store
    from jiuwen_memory.storage._support import wrap_backend

    with wrap_backend("drop shard conditional test table"):
        getattr(store, "_execute")(f"DROP TABLE IF EXISTS {getattr(store, '_table_ref')}")
    store.close()


@pytest.fixture(scope="module")
def redis_store():
    pytest.importorskip("redis")
    store = RedisKVStore(host=REDIS_HOST, port=REDIS_PORT)
    try:
        store.health()
    except Exception as exc:  # pragma: no cover - 环境相关
        pytest.skip(f"redis unavailable on {REDIS_HOST}:{REDIS_PORT}: {exc}")
    yield store


@pytest.fixture(params=["postgres", "redis"])
def store(request):
    if request.param == "postgres":
        return request.getfixturevalue("pg_store")
    return request.getfixturevalue("redis_store")


def test_conditional_create_replace_delete(store) -> None:
    scope = Scope(org=f"shard-it-{uuid.uuid4().hex[:8]}", user="u1")
    key = "/__shard_placement/unit-1"
    first = b'{"status":"pending","token":"t1"}'
    second = b'{"status":"active","token":"t1"}'

    store.create_if_absent(scope, key, first)
    with pytest.raises(ConflictError):
        store.create_if_absent(scope, key, second)

    with pytest.raises(ConflictError):
        store.replace_if_value(scope, key, b"stale", second)
    store.replace_if_value(scope, key, first, second)
    assert store.read_many(scope, [key]) == {key: second}

    assert store.delete_if_value(scope, key, first) is False
    assert store.delete_if_value(scope, key, second) is True
    assert store.read_many(scope, [key]) == {}


def test_read_many_is_partial(store) -> None:
    scope = Scope(org=f"shard-it-{uuid.uuid4().hex[:8]}", user="u1")
    store.create_if_absent(scope, "k1", b"v1")
    assert store.read_many(scope, ["k1", "missing"]) == {"k1": b"v1"}
    assert store.delete_if_value(scope, "k1", b"v1") is True


def test_store_side_clock(store) -> None:
    assert store.now() > 0


def test_concurrent_create_only_one_wins(store) -> None:
    scope = Scope(org=f"shard-it-{uuid.uuid4().hex[:8]}", user="u1")
    key = "/__shard_placement/race"
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            store.create_if_absent(scope, key, b"claimed")
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert store.delete_if_value(scope, key, b"claimed") is True
