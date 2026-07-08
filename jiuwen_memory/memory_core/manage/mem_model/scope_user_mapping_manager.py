# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from typing import Any
from jiuwen_memory.memory_core.manage.mem_model.sql_db_store import SqlDbStore


class ScopeUserMappingManager:
    def __init__(self,
                 sql_db_store: SqlDbStore):
        self.sql_db = sql_db_store
        self.meta_table = "scope_user_mapping"

    async def add(self, user_id: str, scope_id: str, **kwargs):
        """Insert (user_id, scope_id) mapping, atomically ignoring duplicates.

        Uses insert_or_ignore (ON CONFLICT DO NOTHING) instead of the previous
        exist() → write() pattern, which was a non-atomic check-then-act that
        caused UNIQUE constraint races under concurrency.
        """
        data = {
            'user_id': user_id or '',
            'scope_id': scope_id or '',
        }
        await self.sql_db.insert_or_ignore(self.meta_table, data)

    async def delete_by_scope_id(self, scope_id: str) -> bool:
        return await self.sql_db.delete(
            table=self.meta_table,
            conditions={"scope_id": scope_id}
        )

    async def get_by_scope_id(self, scope_id: str) -> list[dict[str, Any]] | None:
        results = await self.sql_db.condition_get(
            table=self.meta_table,
            conditions={"scope_id": [scope_id]},
            columns=None
        )
        return results if results else None
