# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from datetime import datetime, timezone
from typing import Any, List, Tuple

from jiuwen_memory.foundation.llm import Model
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from jiuwen_memory.memory_core.common.base import generate_idx_name, parse_memory_hit_infos
from jiuwen_memory.memory_core.manage.index.base_memory_manager import BaseMemoryManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MiddleTermUnit, BaseMemoryUnit, MemoryType
from jiuwen_memory.memory_core.manage.mem_model.semantic_store import SemanticStore
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


class MiddleTermMemoryManager(BaseMemoryManager):
    """Manages summary memory CRUD with encryption and vector storage"""

    def __init__(self,
                 memory_index: BaseMemoryIndex,
                 crypto_key: bytes):
        self.memory_index = memory_index
        self.crypto_key = crypto_key
        self.mem_type = MemoryType.MIDDLE_TERM_MEMORY.value

    async def add_memories(self, user_id: str, scope_id: str, memories: dict[str, list[BaseMemoryUnit]],
                           llm: Tuple[str, Model] | None = None, **kwargs):
        """add memories in batch."""
        semantic_store = self._get_semantic_store("add", **kwargs)
        valid_units: list[MiddleTermUnit] = []
        for mem_type, memory_list in memories.items():
            if mem_type != self.mem_type:
                continue
            for unit in memory_list:
                if isinstance(unit, MiddleTermUnit):
                    valid_units.append(unit)

        if not valid_units:
            memory_logger.warning(
                "No valid summary units to add",
                event_type=LogEventType.MEMORY_STORE,
                memory_type=self.mem_type,
                user_id=user_id,
                scope_id=scope_id
            )
            return []

        vector_success = await self._add_middle_term_memories_to_vector(
            middleterm_units=valid_units,
            user_id=user_id,
            scope_id=scope_id,
            semantic_store=semantic_store
        )
        if not vector_success:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type=self.mem_type,
                error_msg="middle term memory add to vector store failed",
            )

        return valid_units

    async def update(self, user_id: str, scope_id: str, mem_id: str, new_memory: str, **kwargs):
        """Update middle-term memory by replacing its vector document."""
        semantic_store = self._get_semantic_store("update", **kwargs)
        await self._delete_vector_middle_memory(
            memory_id=[mem_id],
            user_id=user_id,
            scope_id=scope_id,
            semantic_store=semantic_store,
        )

        timestamp = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
        updated_unit = MiddleTermUnit(
            mem_id=mem_id,
            content=new_memory,
            message_mem_id=mem_id,
            timestamp=timestamp,
        )
        vector_success = await self._add_middle_term_memories_to_vector(
            middleterm_units=[updated_unit],
            user_id=user_id,
            scope_id=scope_id,
            semantic_store=semantic_store,
        )
        if not vector_success:
            raise build_error(
                StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                memory_type=self.mem_type,
                error_msg="middle term memory update in vector store failed",
            )
        return True

    async def delete(self, user_id: str, scope_id: str, mem_id: str, **kwargs):
        """delete memory by its id."""
        semantic_store = self._get_semantic_store("delete", **kwargs)

        await self._delete_vector_middle_memory(memory_id=[mem_id],
                                                 user_id=user_id,
                                                 scope_id=scope_id,
                                                 semantic_store=semantic_store)
        return True

    async def delete_by_user_id(self, user_id: str, scope_id: str, **kwargs):
        """Delete all middle-term memories for a user within a scope."""
        semantic_store = self._get_semantic_store("delete", **kwargs)
        await self._delete_vector_store_table(
            user_id=user_id,
            scope_id=scope_id,
            semantic_store=semantic_store,
        )
        return True

    async def get(self, user_id: str, scope_id: str, mem_id: str) -> dict[str, Any] | None:
        """get memory by its id."""
        pass

    async def search(self, user_id: str, scope_id: str, query: str, top_k: int, **kwargs):
        """query memory, return top k results"""
        top_k = 10
        semantic_store = self._get_semantic_store("search", **kwargs)
        results = await self._recall_by_vector(query, user_id, scope_id, semantic_store, top_k)
        return results

    async def _add_summary_memory_to_mem_store(self, user_id: str, scope_id: str, middle_term_unit: MiddleTermUnit):
        """Encrypt and write summary memory to persistent storage."""
        pass

    async def _add_middle_term_memory_to_vector(
        self,
        middleterm_unit: MiddleTermUnit,
        user_id: str,
        scope_id: str,
        semantic_store: SemanticStore
    ) -> bool:
        """Add plaintext summary to vector store for semantic recall."""
        return await self._add_middle_term_memories_to_vector(
            middleterm_units=[middleterm_unit],
            user_id=user_id,
            scope_id=scope_id,
            semantic_store=semantic_store,
        )

    async def _add_middle_term_memories_to_vector(
        self,
        middleterm_units: list[MiddleTermUnit],
        user_id: str,
        scope_id: str,
        semantic_store: SemanticStore
    ) -> bool:
        """Add plaintext summaries to vector store for semantic recall."""
        if not semantic_store:
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type=self.mem_type,
                error_msg="vector store must not be None",
            )
        table_name = generate_idx_name(usr_id=user_id,
                                       scope_id=scope_id,
                                       mem_type=self.mem_type)

        docs = [(unit.message_mem_id, unit.content, unit.timestamp) for unit in middleterm_units]
        return await semantic_store.add_docs(
            docs=docs,
            table_name=table_name,
            scope_id=scope_id,
            is_middle=True
        )

    async def _delete_vector_middle_memory(
            self,
            user_id: str,
            scope_id: str,
            memory_id: List[str],
            semantic_store: SemanticStore
    ):
        """Delete summary memory from vector store by IDs."""
        if not semantic_store:
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type=self.mem_type,
                error_msg="vector store must not be None",
            )
        table_name = generate_idx_name(usr_id=user_id, scope_id=scope_id, mem_type=self.mem_type)
        await semantic_store.delete_docs(memory_id, table_name)

    async def _recall_by_vector(
            self,
            query: str,
            user_id: str,
            scope_id: str,
            semantic_store: SemanticStore,
            top_k: int = 5,
    ):
        """Semantic recall summary memory IDs and similarity scores."""
        table_name = generate_idx_name(usr_id=user_id, scope_id=scope_id, mem_type=MemoryType.MIDDLE_TERM_MEMORY.value)
        memory_hit_info = await semantic_store.search(query=query, table_name=table_name, is_middle=True, top_k=top_k)

        return memory_hit_info

    async def _delete_vector_store_table(
            self,
            user_id: str,
            scope_id: str,
            semantic_store: SemanticStore
    ):
        """Delete entire vector table for user + scope summary memory."""
        if not semantic_store:
            raise build_error(
                StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                memory_type=self.mem_type,
                error_msg="vector store must not be None",
            )
        table_name = generate_idx_name(usr_id=user_id, scope_id=scope_id, mem_type=self.mem_type)
        await semantic_store.delete_table(table_name=table_name)

    def _get_semantic_store(self, operation_type: str, **kwargs) -> SemanticStore:
        """
        Get semantic store from kwargs or raise appropriate error based on operation type.

        Args:
            operation_type: Type of operation being performed ("add", "update", "delete", "search")
            **kwargs: Keyword arguments containing semantic_store

        Returns:
            SemanticStore: The semantic store instance

        Raises:
            Error: If semantic_store is not provided
        """
        semantic_store = kwargs.get('semantic_store')
        if not semantic_store:
            error_codes = {
                "add": StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                "update": StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR,
                "delete": StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR,
                "search": StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
            }
            error_code = error_codes.get(operation_type, StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR)
            raise build_error(
                error_code,
                memory_type=self.mem_type,
                error_msg="semantic_store is required",
            )
        return semantic_store
