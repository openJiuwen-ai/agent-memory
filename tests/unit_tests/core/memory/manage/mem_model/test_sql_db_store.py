#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import asyncio
import uuid
import logging
from pathlib import Path
from enum import StrEnum
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
from jiuwen_memory.memory_core.manage.mem_model.sql_db_store import SqlDbStore
from jiuwen_memory.memory_core.manage.mem_model.db_model import create_tables

logger = logging.getLogger(__name__)


class ContextStoreColumnType(StrEnum):
    TEXT = 'TEXT'
    INTEGER = 'INTEGER'
    REAL = 'REAL'
    BLOB = 'BLOB'
    NUMERIC = 'NUMERIC'


CONTEXT_CONFIG = {
    'table': 'user_message',
    'columns': {
        'user_id': ContextStoreColumnType.TEXT,
        'group_id': ContextStoreColumnType.TEXT,
        'session_id': ContextStoreColumnType.TEXT,
        'message_id': ContextStoreColumnType.TEXT,
        'role': ContextStoreColumnType.TEXT,
        'content': ContextStoreColumnType.TEXT,
        'timestamp': ContextStoreColumnType.TEXT
    }
}

data_list = [
    {
        "user_id": "u1",
        "group_id": "group1",
        "session_id": "s1",
        "message_id": "m1",
        "role": "user",
        "content": "Hello",
        "timestamp": "2025-11-19 09:00:00"
    },
    {
        "user_id": "u1",
        "group_id": "group1",
        "session_id": "s1",
        "message_id": "m2",
        "role": "user",
        "content": "World",
        "timestamp": "2025-11-19 10:00:00"
    },
    {
        "user_id": "u2",
        "group_id": "group2",
        "session_id": "s2",
        "message_id": "m3",
        "role": "assistant",
        "content": "Hi there",
        "timestamp": "2025-11-19 11:00:00"
    }
]


@pytest.fixture(name="test_store")
def store_fixture():
    utc_now = datetime.now(timezone.utc)
    time_str = utc_now.strftime("%Y%m%d%H%M%S")
    uuid_str = uuid.uuid4().hex[:6]
    path = Path(f"./test_sql_db_{time_str}_{uuid_str}.db").resolve()

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    db_store = DefaultDbStore(engine)
    asyncio.run(create_tables(db_store))
    sql_store = SqlDbStore(db_store)

    yield sql_store

    # teardown
    asyncio.run(engine.dispose())
    if path.exists():
        path.unlink()


class TestAsyncSqlDbStore:

    async def async_add(self, store):
        inner_data_list = [
            {
                "user_id": "u1",
                "group_id": "group1",
                "session_id": "s1",
                "message_id": "m1",
                "role": "user",
                "content": "Hello",
                "timestamp": "2025-11-19 09:00:00"
            },
            {
                "user_id": "u1",
                "group_id": "group1",
                "session_id": "s1",
                "message_id": "m2",
                "role": "user",
                "content": "World",
                "timestamp": "2025-11-19 10:00:00"
            },
            {
                "user_id": "u2",
                "group_id": "group2",
                "session_id": "s2",
                "message_id": "m3",
                "role": "assistant",
                "content": "Hi there",
                "timestamp": "2025-11-19 11:00:00"
            }
        ]

        for data in inner_data_list:
            assert await store.write(CONTEXT_CONFIG["table"], data)

    async def async_insert_or_update_new_row(self, store):
        """insert_or_update on a row that does not exist → inserts it."""
        data = {
            'table_name': 'user_message',
            'schema_version': '1',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data,
            index_elements=["table_name"],
        )
        assert result is True
        row = await store.condition_get(
            'memory_meta',
            conditions={"table_name": ['user_message']},
        )
        assert row is not None
        assert row[0]["table_name"] == "user_message"
        assert row[0]["schema_version"] == "1"

    async def async_insert_or_update_conflict_updates(self, store):
        """insert_or_update on an existing PK → updates non-PK columns."""
        data_v1 = {
            'table_name': 'user_message',
            'schema_version': '1',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v1,
            index_elements=["table_name"],
        )
        assert result is True

        # Upgrade schema_version from 1 → 2 (PK = table_name stays the same)
        data_v2 = {
            'table_name': 'user_message',
            'schema_version': '2',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v2,
            index_elements=["table_name"],
        )
        assert result is True

        row = await store.condition_get(
            'memory_meta',
            conditions={"table_name": ['user_message']},
        )
        assert row is not None
        assert len(row) == 1  # only one row, not two
        assert row[0]["schema_version"] == "2"

    async def async_insert_or_update_auto_pk(self, store):
        """insert_or_update without explicit index_elements → auto-detect PK."""
        data_v1 = {
            'table_name': 'scope_mapping',
            'schema_version': '1',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v1,
        )
        assert result is True

        data_v2 = {
            'table_name': 'scope_mapping',
            'schema_version': '5',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v2,
        )
        assert result is True

        row = await store.condition_get(
            'memory_meta',
            conditions={"table_name": ['scope_mapping']},
        )
        assert row is not None
        assert row[0]["schema_version"] == "5"

    async def async_insert_or_update_custom_update_fields(self, store):
        """insert_or_update with custom update_fields → only listed fields updated on conflict."""
        data_v1 = {
            'table_name': 'custom_table',
            'schema_version': '1',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v1,
            index_elements=["table_name"],
        )
        assert result is True

        # Conflict with same table_name but different schema_version;
        # Only update schema_version (the default behavior when update_fields=data).
        data_v2 = {
            'table_name': 'custom_table',
            'schema_version': '3',
        }
        result = await store.insert_or_update(
            table='memory_meta',
            data=data_v2,
            index_elements=["table_name"],
            update_fields={"schema_version": "3"},
        )
        assert result is True

        row = await store.condition_get(
            'memory_meta',
            conditions={"table_name": ['custom_table']},
        )
        assert row is not None
        assert row[0]["schema_version"] == "3"

    async def async_get(self, store):
        filters = {}
        filters['message_id'] = ["m1"]
        row = await store.condition_get(
            CONTEXT_CONFIG["table"],
            conditions=filters
        )
        assert row is not None
        assert row[0]["user_id"] == "u1"
        assert row[0]["content"] == "Hello"

    async def async_get_with_sort(self, store):
        """Verify that get_with_sort correctly performs query sorting."""

        table = CONTEXT_CONFIG["table"]

        rows = await store.get_with_sort(
            table=table,
            filters={"user_id": "u1"},
            sort_by="timestamp",
            order="ASC",
            limit=10
        )
        assert len(rows) == 2
        assert rows[0]["message_id"] == "m1"
        assert rows[1]["message_id"] == "m2"

        # user_id = u2
        rows_u2 = await store.get_with_sort(
            table=table,
            filters={"user_id": "u2"},
            sort_by="timestamp",
            order="ASC",
            limit=10
        )

        assert len(rows_u2) >= 1
        assert rows_u2[0]["message_id"] == "m3"

    async def async_exist(self, store):
        all_data = await store.get_with_sort(
            table=CONTEXT_CONFIG["table"], filters={})
        logger.info(f"all_data: {all_data}")
        assert len(all_data) > 0
        assert await store.exist(CONTEXT_CONFIG["table"], {"message_id": "m1"})
        assert not(await store.exist(CONTEXT_CONFIG["table"], {"message_id": "not_exist"}))
        assert (await store.exist(CONTEXT_CONFIG["table"], {"user_id": "u1", "content": "Hello"}))
        assert not(await store.exist(CONTEXT_CONFIG["table"], {"user_id": "u1", "content": "foo"}))
        assert not(await store.exist(CONTEXT_CONFIG["table"], {"user_id": "uX", "content": "bar"}))

    async def async_update(self, store):
        assert await store.update(CONTEXT_CONFIG["table"], {"message_id": "m1"}, {"content": "hi"})
        row = await store.condition_get(CONTEXT_CONFIG["table"], {"message_id": ["m1"]})
        assert (row[0]["content"] == "hi")
        assert await store.update(CONTEXT_CONFIG["table"], {"message_id": ["m2", "m3"]}, {"content": "batch"})
        row2 = await store.condition_get(CONTEXT_CONFIG["table"], {"message_id": ["m2"]})
        row3 = await store.condition_get(CONTEXT_CONFIG["table"], {"message_id": ["m3"]})
        assert (row2[0]["content"] == "batch")
        assert (row3[0]["content"] == "batch")

    async def async_delete(self, store):
        assert await store.delete(CONTEXT_CONFIG["table"], {"message_id": "m1"})
        row = await store.condition_get(CONTEXT_CONFIG["table"], {"message_id": ["m1"]})
        assert (row == [])

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="need aiosqlite")
    def test_basic(self, test_store):
        asyncio.run(self.async_add(test_store))
        asyncio.run(self.async_get(test_store))
        asyncio.run(self.async_get_with_sort(test_store))
        asyncio.run(self.async_exist(test_store))
        asyncio.run(self.async_update(test_store))
        asyncio.run(self.async_delete(test_store))

    @pytest.mark.asyncio
    async def test_insert_or_update_new_row(self, test_store):
        await self.async_insert_or_update_new_row(test_store)

    @pytest.mark.asyncio
    async def test_insert_or_update_conflict_updates(self, test_store):
        await self.async_insert_or_update_conflict_updates(test_store)

    @pytest.mark.asyncio
    async def test_insert_or_update_auto_pk(self, test_store):
        await self.async_insert_or_update_auto_pk(test_store)

    @pytest.mark.asyncio
    async def test_insert_or_update_custom_update_fields(self, test_store):
        await self.async_insert_or_update_custom_update_fields(test_store)
