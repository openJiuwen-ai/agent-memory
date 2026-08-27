# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
import base64
import json
import time
from typing import (
    Dict,
    List,
    Optional,
)

from sqlalchemy import (
    Column,
    delete,
    select,
    String,
    Text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    AsyncSession,
)
from sqlalchemy.orm import DeclarativeBase

from jiuwen_memory.foundation.store.base_kv_store import (
    BasedKVStorePipeline,
    BaseKVStore,
)

EXCLUSIVE_EXPIRY_KEY = "exclusive_expiry"
EXCLUSIVE_VALUE_KEY = "exclusive_value"


class Base(DeclarativeBase):
    pass


class KVStoreTable(Base):
    __tablename__ = "kv_store"
    key = Column(String(255), primary_key=True)
    value = Column(Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False)


class DbBasedKVStore(BaseKVStore):
    _BYTES_PREFIX = "__BYTES__:"

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.async_session = async_sessionmaker(
            self.engine, expire_on_commit=False,
            class_=AsyncSession
        )
        self._table_created = False
        self._lock = asyncio.Lock()

    def _get_upsert_stmt(self, key: str, value: str):
        """返回方言对应的 upsert 语句，GaussDB 返回 None（走手动 upsert）。"""
        dialect_name = self.engine.dialect.name

        if dialect_name == "mysql":
            stmt = (
                mysql_insert(KVStoreTable)
                .values(key=key, value=value)
                .on_duplicate_key_update(value=value)
            )
        elif dialect_name == "gaussdb":
            # GaussDB 不支持 ON CONFLICT 也不支持 ON DUPLICATE KEY UPDATE
            # 返回 None，由调用方走手动 UPDATE + INSERT 路径
            stmt = None
        else:
            stmt = (
                sqlite_insert(KVStoreTable)
                .values(key=key, value=value)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value}
                )
            )
        return stmt

    async def _manual_upsert(self, session, key: str, value: str):
        """手动 upsert（先 SELECT 判断，存在则 UPDATE，不存在则 INSERT），用于 GaussDB。

        GaussDB 的 async_gaussdb 驱动抛 UniqueViolationError 后，
        SQLAlchemy 的 asyncpg 异常映射会因缺少 asyncpg 模块而崩溃，
        因此不能用 try-INSERT-catch 方案。改为先 SELECT 判断再 INSERT/UPDATE。
        并发问题由调用方的应用层锁（_task_guard_lock）和 exclusive_set 的 SELECT 检查缓解。
        """
        from sqlalchemy import text
        select_sql = text("SELECT 1 FROM kv_store WHERE key = :key LIMIT 1")
        update_sql = text("UPDATE kv_store SET value = :value WHERE key = :key")
        insert_sql = text(
            "INSERT INTO kv_store (key, value) VALUES (:key, :value)"
        )
        result = await session.execute(select_sql, {"key": key})
        if result.first() is not None:
            await session.execute(update_sql, {"key": key, "value": value})
        else:
            try:
                await session.execute(insert_sql, {"key": key, "value": value})
            except IntegrityError:
                # 并发场景：另一事务已插入，转 UPDATE
                await session.execute(update_sql, {"key": key, "value": value})

    def _encode_value(self, value: str | bytes) -> str:
        """Encode value to string for database storage."""
        if isinstance(value, bytes):
            return self._BYTES_PREFIX + base64.b64encode(value).decode('utf-8')
        return value

    def _decode_value(self, value: str) -> str | bytes:
        """Decode value from database storage."""
        if value.startswith(self._BYTES_PREFIX):
            encoded = value[len(self._BYTES_PREFIX):]
            return base64.b64decode(encoded)
        return value

    async def set(self, key: str, value: str | bytes):
        await self._create_table_if_not_exist()
        encoded_value = self._encode_value(value)
        async with self.async_session() as session:
            async with session.begin():
                stmt = self._get_upsert_stmt(key, encoded_value)
                if stmt is not None:
                    await session.execute(stmt)
                else:
                    await self._manual_upsert(session, key, encoded_value)

    async def exclusive_set(
            self, key: str, value: str | bytes, expiry: Optional[int] = None
    ) -> bool:
        """原子性设置独占锁。

        GaussDB 路径改为先 INSERT（失败转 UPDATE），避免 check-then-set 竞态。
        MySQL/SQLite 走方言原子 upsert，天然无竞态。
        """
        await self._create_table_if_not_exist()
        now = time.time()
        expire_at = now + expiry if expiry else None
        encoded_value = self._encode_value(value)
        val = json.dumps({EXCLUSIVE_VALUE_KEY: encoded_value, EXCLUSIVE_EXPIRY_KEY: expire_at})

        async with self.async_session() as session:
            async with session.begin():
                # 先检查是否已被持有（未过期）
                stmt = select(KVStoreTable).where(
                    KVStoreTable.key == key
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    try:
                        data = json.loads(row.value)
                        old_expire = data.get(EXCLUSIVE_EXPIRY_KEY)
                        if old_expire is None or old_expire > now:
                            return False
                    except json.JSONDecodeError:
                        return False

                # 原子 upsert（MySQL/SQLite 走方言，GaussDB 走手动）
                stmt = self._get_upsert_stmt(key, val)
                if stmt is not None:
                    await session.execute(stmt)
                else:
                    await self._manual_upsert(session, key, val)
                return True

    async def renew_exclusive(
        self, key: str, value: str | bytes, expiry: Optional[int] = None
    ) -> bool:
        """
        Renew the expiry of an exclusively-held key, only if the current
        value matches ``value``. Returns True on success, False otherwise.
        """
        await self._create_table_if_not_exist()
        now = time.time()
        async with self.async_session() as session:
            async with session.begin():
                stmt = select(KVStoreTable).where(KVStoreTable.key == key)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return False
                try:
                    data = json.loads(row.value)
                    stored_value = data.get(EXCLUSIVE_VALUE_KEY)
                    old_expire = data.get(EXCLUSIVE_EXPIRY_KEY)
                except json.JSONDecodeError:
                    return False
                # Key already expired → cannot renew
                if old_expire is not None and old_expire <= now:
                    return False
                # Value mismatch → caller doesn't hold the lock
                encoded_expected = self._encode_value(value)
                if stored_value != encoded_expected:
                    return False
                # Renew: update expiry
                expire_at = now + expiry if expiry else None
                new_payload = json.dumps({
                    EXCLUSIVE_VALUE_KEY: stored_value,
                    EXCLUSIVE_EXPIRY_KEY: expire_at,
                })
                stmt = self._get_upsert_stmt(key, new_payload)
                if stmt is not None:
                    await session.execute(stmt)
                else:
                    await self._manual_upsert(session, key, new_payload)
                return True

    async def get(self, key: str) -> str | bytes | None:
        await self._create_table_if_not_exist()
        async with self.async_session() as session:
            stmt = select(KVStoreTable).where(KVStoreTable.key == key)
            rec = (await session.execute(stmt)).scalar_one_or_none()
            if rec is None:
                return None
            try:
                result_dict = json.loads(rec.value)
                if not isinstance(result_dict, dict):
                    return rec.value
                if EXCLUSIVE_EXPIRY_KEY in result_dict:
                    return result_dict.get(EXCLUSIVE_VALUE_KEY, "")
            except json.JSONDecodeError:
                pass
            return self._decode_value(rec.value)

    async def exists(self, key: str) -> bool:
        await self._create_table_if_not_exist()
        async with self.async_session() as session:
            stmt = select(KVStoreTable).where(KVStoreTable.key == key)
            rec = (await session.execute(stmt)).scalar_one_or_none()
            return rec is not None

    async def delete(self, key: str):
        await self._create_table_if_not_exist()
        async with self.async_session() as session:
            async with session.begin():
                await session.execute(
                    delete(KVStoreTable).where(KVStoreTable.key == key)
                )

    async def get_by_prefix(self, prefix: str) -> dict[str, str | bytes]:
        await self._create_table_if_not_exist()
        async with self.async_session() as session:
            stmt = select(KVStoreTable).where(
                KVStoreTable.key.startswith(prefix)
            )
            rows = (await session.execute(stmt)).scalars().all()
            result: Dict[str, str | bytes] = {}
            for rec in rows:
                result[rec.key] = self._decode_value(rec.value)
            return result

    async def delete_by_prefix(
            self, prefix: str, batch_size: Optional[int] = None
    ):
        """
        Remove all key-value pairs whose keys start with the given prefix.

        Args:
            prefix (str): The string prefix to match against existing keys.
            batch_size (Optional[int]): Optional batch size for deletion. If None or <= 0,
                all matching keys are deleted in a single operation. Default is None.
        """
        await self._create_table_if_not_exist()
        async with self.async_session() as session:
            async with session.begin():
                if batch_size is None or batch_size <= 0:
                    # Delete all at once
                    await session.execute(
                        delete(KVStoreTable).where(
                            KVStoreTable.key.startswith(prefix)
                        )
                    )
                else:
                    # Delete in batches
                    # First, query all keys matching the prefix
                    stmt = select(KVStoreTable.key).where(
                        KVStoreTable.key.startswith(prefix)
                    )
                    rows = (await session.execute(stmt)).scalars().all()
                    keys_to_delete = list(rows)

                    # Delete in batches
                    if keys_to_delete:
                        for i in range(0, len(keys_to_delete), batch_size):
                            batch = keys_to_delete[i:i + batch_size]
                            stmt = delete(KVStoreTable).where(
                                KVStoreTable.key.in_(batch)
                            )
                            await session.execute(stmt)

    async def mget(self, keys: List[str]) -> List[str | bytes | None]:
        await self._create_table_if_not_exist()
        if not keys:
            return []
        async with self.async_session() as session:
            stmt = select(KVStoreTable).where(KVStoreTable.key.in_(keys))
            rows = (await session.execute(stmt)).scalars().all()
            lookup: Dict[str, str | bytes] = {}
            for rec in rows:
                lookup[rec.key] = self._decode_value(rec.value)
            return [lookup.get(k) for k in keys]

    async def batch_delete(self, keys: List[str], batch_size: Optional[int] = None) -> int:
        """
        Delete a batch of keys.

        Args:
            keys (List[str]): List of keys to delete.
            batch_size (Optional[int]): Optional batch size for deletion. If None or <= 0,
                all keys are deleted in a single operation. Default is None.

        Returns:
            int: Number of keys actually deleted.
        """
        await self._create_table_if_not_exist()
        if not keys:
            return 0

        async with self.async_session() as session:
            async with session.begin():
                if batch_size is None or batch_size <= 0:
                    # Delete all at once
                    stmt = delete(KVStoreTable).where(
                        KVStoreTable.key.in_(keys)
                    )
                    result = await session.execute(stmt)
                    return result.rowcount or 0
                else:
                    # Delete in batches
                    total_deleted = 0
                    for i in range(0, len(keys), batch_size):
                        batch = keys[i:i + batch_size]
                        stmt = delete(KVStoreTable).where(
                            KVStoreTable.key.in_(batch)
                        )
                        result = await session.execute(stmt)
                        total_deleted += result.rowcount or 0
                    return total_deleted

    async def _create_table_if_not_exist(self) -> None:
        if self._table_created:
            return
        async with self._lock:
            if self._table_created:
                return
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self._table_created = True

    def pipeline(self):
        """
        Create a pipeline-like interface for batch operations.

        Returns:
            openjiuwen.core.foundation.store.base_kv_store.BasedKVStorePipeline: A pipeline object for batch operations.
        """

        async def execute(operations):
            await self._create_table_if_not_exist()
            results = []
            async with self.async_session() as session:
                async with session.begin():
                    # Group operations by type for efficient batch processing
                    set_ops = []
                    get_keys = []
                    exists_keys = []
                    for op in operations:
                        op_type = op[0]
                        if op_type == 'set':
                            set_ops.append((op[1], op[2]))
                        elif op_type == 'get':
                            get_keys.append(op[1])
                        elif op_type == 'exists':
                            exists_keys.append(op[1])

                    # Batch set operations
                    if set_ops:
                        for key, value in set_ops:
                            encoded_value = self._encode_value(value)
                            stmt = self._get_upsert_stmt(key, encoded_value)
                            if stmt is not None:
                                await session.execute(stmt)
                            else:
                                await self._manual_upsert(session, key, encoded_value)

                    # Batch get operations
                    get_results = {}
                    if get_keys:
                        stmt = select(KVStoreTable).where(
                            KVStoreTable.key.in_(get_keys)
                        )
                        rows = (await session.execute(stmt)).scalars().all()
                        for rec in rows:
                            get_results[rec.key] = (
                                self._decode_value(rec.value)
                            )

                    # Batch exists operations
                    exists_results = {}
                    if exists_keys:
                        stmt = select(KVStoreTable).where(
                            KVStoreTable.key.in_(exists_keys)
                        )
                        rows = (await session.execute(stmt)).scalars().all()
                        for rec in rows:
                            exists_results[rec.key] = True
                        # Mark non-existent keys as False
                        for key in exists_keys:
                            if key not in exists_results:
                                exists_results[key] = False

                    # Build results in the order operations were added
                    for op in operations:
                        op_type = op[0]
                        key = op[1]
                        if op_type == 'set':
                            results.append(None)
                        elif op_type == 'get':
                            results.append(get_results.get(key))
                        elif op_type == 'exists':
                            results.append(exists_results.get(key, False))

            return results

        return BasedKVStorePipeline(execute)
