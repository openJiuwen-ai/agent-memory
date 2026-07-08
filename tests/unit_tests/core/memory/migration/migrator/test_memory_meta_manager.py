# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from unittest.mock import AsyncMock

import pytest

from jiuwen_memory.memory_core.manage.mem_model.sql_db_store import SqlDbStore
from jiuwen_memory.memory_core.migration.migrator.memory_meta_manager import (
    MemoryMetaManager,
)


@pytest.fixture(name="mock_sql_db")
def mock_sql_db_fixture():
    sql_db = AsyncMock(spec=SqlDbStore)
    sql_db.insert_or_update = AsyncMock(return_value=True)
    return sql_db


@pytest.fixture(name="meta_manager")
def meta_manager_fixture(mock_sql_db):
    return MemoryMetaManager(mock_sql_db)


class TestMemoryMetaManagerAdd:

    @pytest.mark.asyncio
    async def test_add_first_insert(self, meta_manager, mock_sql_db):
        """First add for a table_name → insert_or_update called with data."""
        await meta_manager.add("user_message", "1")
        mock_sql_db.insert_or_update.assert_called_once_with(
            table="memory_meta",
            data={"table_name": "user_message", "schema_version": "1"},
            index_elements=["table_name"],
        )

    @pytest.mark.asyncio
    async def test_add_version_upgrade(self, meta_manager, mock_sql_db):
        """
        Upgrading schema version (v1 → v2) → insert_or_update called,
        which will atomically update the existing row's schema_version.
        """
        await meta_manager.add("user_message", "1")
        mock_sql_db.insert_or_update.assert_called_once_with(
            table="memory_meta",
            data={"table_name": "user_message", "schema_version": "1"},
            index_elements=["table_name"],
        )

        # Upgrade version
        await meta_manager.add("user_message", "2")
        # Second call should also use insert_or_update, not write()
        assert mock_sql_db.insert_or_update.call_count == 2
        mock_sql_db.insert_or_update.assert_called_with(
            table="memory_meta",
            data={"table_name": "user_message", "schema_version": "2"},
            index_elements=["table_name"],
        )

    @pytest.mark.asyncio
    async def test_add_empty_table_name_returns_early(self, meta_manager, mock_sql_db):
        """Empty table_name → early return, no DB call."""
        await meta_manager.add("", "1")
        mock_sql_db.insert_or_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_empty_schema_version_returns_early(self, meta_manager, mock_sql_db):
        """Empty schema_version → early return, no DB call."""
        await meta_manager.add("user_message", "")
        mock_sql_db.insert_or_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_no_longer_calls_exist_or_write(self, meta_manager, mock_sql_db):
        """add() should not call exist() or write() — replaced by insert_or_update."""
        mock_sql_db.exist = AsyncMock(return_value=False)
        mock_sql_db.write = AsyncMock(return_value=True)

        await meta_manager.add("user_message", "1")

        mock_sql_db.exist.assert_not_called()
        mock_sql_db.write.assert_not_called()
