# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import logging
from typing import Any, Dict, List

from sqlalchemy import insert, update, select, delete, Table, MetaData, and_, or_, desc, asc
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error

from jiuwen_memory.foundation.store import BaseDbStore
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType



class SqlDbStore:
    def __init__(self, db_store: BaseDbStore):
        self.db_store = db_store
        self._async_table_cache: dict[str, Table] = {}
        self.async_session = async_sessionmaker(
            bind=self.db_store.get_async_engine(),
            expire_on_commit=False,
            class_=AsyncSession)

    async def write(self, table: str, data: dict) -> bool:
        t = await self.get_table(table)
        stmt = insert(t).values(**data)
        try:
            async with self.async_session() as session:
                async with session.begin():
                    await session.execute(stmt)
                return True
        except IntegrityError as e:
            # UNIQUE / PK constraint violation — must propagate so callers
            # can distinguish "write failed due to conflict" from "write
            # succeeded".  Swallowing this was the root cause of silent
            # data loss (request returned 200 but message was never stored).
            memory_logger.warning(
                "Write failed due to UNIQUE constraint violation",
                event_type=LogEventType.MEMORY_STORE,
                exception=str(e),
                metadata={"table_name": table}
            )
            raise
        except Exception as e:
            memory_logger.error(
                "Write failed",
                event_type=LogEventType.MEMORY_STORE,
                exception=str(e),
                metadata={"table_name": table}
            )
            raise

    async def insert_or_update(self, table: str, data: dict,
                                index_elements: list[str] | None = None,
                                update_fields: dict | None = None) -> bool:
        """Insert a row, updating it on UNIQUE / primary-key conflict.

        Uses ``INSERT … ON CONFLICT DO UPDATE`` (SQLite / PostgreSQL /
        GaussDB) or ``INSERT … ON DUPLICATE KEY UPDATE`` (MySQL).  This is
        an atomic upsert that eliminates the non-atomic exist() → write()
        race window.

        Args:
            table: Name of the target table (will be reflected).
            data: Column values to insert.
            index_elements: Columns that define the conflict target.
                Defaults to the table's primary-key columns (auto-detected
                from the reflected Table).
            update_fields: Columns to update on conflict.
                Defaults to ``data`` itself (i.e. every supplied column is
                overwritten with the new value).

        Returns:
            True if the row was inserted or updated successfully.
            False if an unexpected error occurred (logged at ERROR level).
        """
        t = await self.get_table(table)
        dialect_name = self.db_store.get_async_engine().dialect.name

        if index_elements is None:
            pk_cols = [col.name for col in t.primary_key.columns]
            if not pk_cols:
                memory_logger.error(
                    "insert_or_update requires a conflict target but the "
                    "reflected table has no primary key",
                    event_type=LogEventType.MEMORY_STORE,
                    metadata={"table_name": table}
                )
                return False
            index_elements = pk_cols

        if update_fields is None:
            update_fields = data

        if dialect_name == "mysql":
            stmt = (
                mysql_insert(t)
                .values(**data)
                .on_duplicate_key_update(**update_fields)
            )
        else:
            # SQLite, PostgreSQL, GaussDB all use the same
            # on_conflict_do_update syntax via sqlite_insert.
            stmt = (
                sqlite_insert(t)
                .values(**data)
                .on_conflict_do_update(
                    index_elements=index_elements,
                    set_=update_fields,
                )
            )

        try:
            async with self.async_session() as session:
                async with session.begin():
                    await session.execute(stmt)
            return True
        except Exception as e:
            memory_logger.error(
                "insert_or_update failed",
                event_type=LogEventType.MEMORY_STORE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return False

    async def insert_or_ignore(self, table: str, data: dict) -> bool:
        """Insert a row, silently ignoring UNIQUE / primary-key conflicts.

        Uses ``INSERT … ON CONFLICT DO NOTHING`` (SQLite) or
        ``INSERT IGNORE …`` (MySQL).  Concurrent writers targeting the
        same key will not raise IntegrityError and will not cause silent
        data loss.

        Returns:
            True if the row was actually inserted.
            False if the row already existed (conflict ignored) **or** if an
            unexpected error occurred (logged at ERROR level).
        """
        t = await self.get_table(table)
        dialect_name = self.db_store.get_async_engine().dialect.name

        if dialect_name == "mysql":
            # INSERT IGNORE: skip rows that violate UNIQUE/PK constraints
            # without raising an error or performing a no-op UPDATE.
            stmt = mysql_insert(t).prefix_with("IGNORE").values(**data)
        else:
            stmt = sqlite_insert(t).values(**data).on_conflict_do_nothing()

        try:
            async with self.async_session() as session:
                async with session.begin():
                    result = await session.execute(stmt)
                # rowcount semantics:
                #   SQLite ON CONFLICT DO NOTHING → 0 when ignored, 1 when inserted
                #   MySQL INSERT IGNORE → 1 when inserted, 0 when ignored
                #   (both dialects agree: > 0 means new row, 0 means conflict skipped)
                return result.rowcount > 0
        except Exception as e:
            memory_logger.error(
                "insert_or_ignore failed",
                event_type=LogEventType.MEMORY_STORE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return False

    async def get(self, table: str, record_id: str, columns: list[str] | None = None) -> dict[str, Any] | None:
        if columns is None:
            columns = []
        try:
            t = await self.get_table(table)
            if columns:
                cols = [t.c[col] for col in columns]
                stmt = select(*cols)
            else:
                stmt = select(t)
            stmt = stmt.where(t.c.id == record_id)
            async with self.async_session() as session:
                async with session.begin():
                    execute_result = await session.execute(stmt)
                    row = execute_result.mappings().first()
                    return dict(row) if row else None
        except Exception as e:
            memory_logger.error(
                "Failed to get data",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                metadata={"table_name": table, "record_id": record_id}
            )
            return None

    async def get_with_sort(self, table: str, filters: Dict[str, Any], sort_by: str = "timestamp",
                             order: str = "ASC", limit: int = 100) -> List[Dict[str, Any]]:
        try:
            t = await self.get_table(table)
            if sort_by not in t.c:
                raise build_error(
                    StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                    memory_type="message",
                    error_msg=f"sort column '{sort_by}' does not exist "
                              f"in db store table '{table}'"
                )
            clauses = [
                t.c[col] == val for col, val in filters.items() if col in t.c
            ]
            stmt = select(t)
            if clauses:
                stmt = stmt.where(and_(*clauses))
            if order.upper() == "DESC":
                stmt = stmt.order_by(desc(t.c[sort_by]))
            else:
                stmt = stmt.order_by(asc(t.c[sort_by]))
            stmt = stmt.limit(limit)
            async with self.async_session() as session:
                async with session.begin():
                    execute_result = await session.execute(stmt)
                    result = execute_result.mappings().fetchall()
                    return [dict(row) for row in result]
        except Exception as e:
            memory_logger.error(
                "Failed to fetch filtered and sorted data",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return []

    async def exist(self, table: str, conditions: Dict[str, Any]) -> bool:
        t = await self.get_table(table)
        clauses = [t.c[col] == val for col, val in conditions.items()]
        stmt = select(1).where(and_(*clauses))
        async with self.async_session() as session:
            async with session.begin():
                execute_result = await session.execute(stmt)
                return execute_result.first() is not None

    async def batch_get(self, table: str, conditions_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        t = await self.get_table(table)
        clauses = [or_(*[t.c[col] == val for col, val in cond.items()]) for cond in conditions_list]
        stmt = select(t).where(or_(*clauses)) if clauses else select(t)
        async with self.async_session() as session:
            async with session.begin():
                execute_result = await session.execute(stmt)
                return [dict(r) for r in execute_result.mappings().fetchall()]

    async def condition_get(self, table: str, conditions: Dict[str, List[Any]],
                             columns: List[str] | None = None) -> List[Dict[str, Any]] | None:
        if columns is None:
            columns = []
        try:
            t: Table = await self.get_table(table)
            stmt = (
                select(t) if not columns
                else select(*[t.c[col] for col in columns])
            )
            clause_list = []
            for col, values in conditions.items():
                if not isinstance(values, list):
                    raise build_error(
                        StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                        memory_type="message",
                        error_msg=f"db store condition[{col}] must be a list, "
                                  f"(got {type(conditions[col]).__name__})",
                    )
                clause_list.append(t.c[col].in_(values))
            if clause_list:
                stmt = stmt.where(and_(*clause_list))
            async with self.async_session() as session:
                async with session.begin():
                    execute_result = await session.execute(stmt)
                    rows = execute_result.mappings().fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            memory_logger.error(
                "Failed to get data via condition_get",
                event_type=LogEventType.MEMORY_RETRIEVE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return None

    async def update(self, table: str, conditions: dict, data: dict) -> bool:
        t = await self.get_table(table)
        clauses = [t.c[col].in_(vals) if isinstance(vals, list) else t.c[col] == vals
                   for col, vals in conditions.items()]
        stmt = update(t).where(and_(*clauses)).values(**data)
        try:
            async with self.async_session() as session:
                async with session.begin():
                    await session.execute(stmt)
            return True
        except Exception as e:
            memory_logger.error(
                "Update failed",
                event_type=LogEventType.MEMORY_UPDATE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return False

    async def delete(self, table: str, conditions: dict) -> bool:
        t = await self.get_table(table)
        clauses = [t.c[col].in_(vals) if isinstance(vals, list) else t.c[col] == vals
                   for col, vals in conditions.items()]
        stmt = delete(t).where(and_(*clauses))
        try:
            async with self.async_session() as session:
                async with session.begin():
                    await session.execute(stmt)
            return True
        except Exception as e:
            memory_logger.error(
                "Delete failed",
                event_type=LogEventType.MEMORY_DELETE,
                exception=str(e),
                metadata={"table_name": table}
            )
            return False

    async def delete_table(self, table_name: str) -> bool:
        try:
            metadata = MetaData()
            t = Table(table_name, metadata)
            async with self.db_store.get_async_engine().begin() as conn:
                await conn.run_sync(t.drop, checkfirst=True)
            return True
        except Exception as e:
            memory_logger.error(
                "Delete table failed",
                event_type=LogEventType.MEMORY_DELETE,
                exception=str(e),
                metadata={"table_name": table_name}
            )
            return False

    def invalidate_table_cache(self, table_name: str) -> None:
        """Remove a table from the reflection cache so the next get_table call re-reflects."""
        self._async_table_cache.pop(table_name, None)

    async def get_table(self, table_name: str) -> Table:
        if table_name in self._async_table_cache:
            return self._async_table_cache[table_name]
        metadata = MetaData()
        async with self.db_store.get_async_engine().connect() as conn:
            def sync_reflect(sync_conn):
                return Table(table_name, metadata, autoload_with=sync_conn)

            table = await conn.run_sync(sync_reflect)
            self._async_table_cache[table_name] = table
            return table
