# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Knowledge stores for dreaming.

Phase-1 only produces ``MemoryUnitKnowledgeStore``: it converts extracted
knowledge into ``FragmentMemoryUnit`` and writes them through the *normal*
memory write path (``write_manager`` → ``MemUpdateChecker`` →
``memory_index``). Storage format, dedup and retrieval are therefore identical
to ordinary memories — dreaming adds no new memory type and no new field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock
from jiuwen_memory.memory_core.manage.mem_model.data_id_manager import DataIdManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import FragmentMemoryUnit, MemoryType
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType

# The three fragment memory types dreaming is allowed to emit. Mirrors
# FRAGMENT_MEMORY_TYPE in fragment_memory_manager.py — anything else (VARIABLE /
# SUMMARY / UNKNOWN) would be filtered out by the write path, so we drop it here.
_ALLOWED_MEM_TYPES = {
    MemoryType.USER_PROFILE,
    MemoryType.SEMANTIC_MEMORY,
    MemoryType.EPISODIC_MEMORY,
}


@dataclass
class KnowledgeItem:
    mem_type: str               # canonical enum value: "user_profile" | "semantic_memory" | "episodic_memory"
    content: str                # the memory statement, stored as-is (consistent with online extraction)
    source_session_id: str
    is_important: bool = False  # protected from Ebbinghaus forgetting


def _to_mem_type(value: str) -> Optional[MemoryType]:
    """str → MemoryType, restricted to the fragment types. None if invalid."""
    try:
        mem_type = MemoryType(value)
    except ValueError:
        return None
    return mem_type if mem_type in _ALLOWED_MEM_TYPES else None


class MemoryUnitKnowledgeStore:
    """
    Phase-1 sole output: reuse the normal memory write path; knowledge only
    goes into the vector store.
    """

    def __init__(self, write_manager, kv_store, llm, user_id: str, scope_id: str,
                 prepare_write=None) -> None:
        self._write_manager = write_manager
        self._kv_store = kv_store
        self._llm = llm
        self._user_id = user_id
        self._scope_id = scope_id
        # Optional async hook run inside the write lock, right before add_memories.
        # Used to apply the scope-specific embedding model onto the shared memory_index
        # (mirrors add_messages' _apply_scope_embedding); None = no-op.
        self._prepare_write = prepare_write
        # DataIdManager is stateless (time+secrets+hash); building it here needs no injection.
        self._data_id = DataIdManager()

    async def promote(self, items: List[KnowledgeItem]) -> int:
        """
        Write knowledge items to the vector store. Returns the number of
        units actually persisted (after MemUpdateChecker dedup).
        """
        if not items:
            return 0

        # 1) str → MemoryType (drop invalid); build FragmentMemoryUnit; 2) group by mem_type.value
        memories: dict[str, list[FragmentMemoryUnit]] = {}
        attempted = 0
        for item in items:
            mem_type = _to_mem_type(item.mem_type)
            if mem_type is None:
                memory_logger.warning(
                    "dreaming: drop item with invalid mem_type '%s'",
                    item.mem_type,
                    event_type=LogEventType.MEMORY_STORE,
                    user_id=self._user_id,
                    scope_id=self._scope_id,
                )
                continue

            text = (item.content or "").strip()
            if not text:
                continue

            mem_id = await self._data_id.generate_next_id(self._user_id)
            unit = FragmentMemoryUnit(
                mem_type=mem_type,
                mem_id=mem_id,
                content=text,
                message_mem_id=item.source_session_id,   # provenance marker → MemoryDoc.fields.source_id
                timestamp="",                            # empty → write path stamps now
                is_important=item.is_important,
            )
            memories.setdefault(mem_type.value, []).append(unit)
            attempted += 1

        if not memories:
            return 0

        # 3) hold the same user lock add_messages uses, then go through the normal write path.
        #    The write path is a lock-free read-modify-write; concurrency safety is the caller's job.
        async with DistributedLock(self._kv_store, f"user/{self._user_id}"):
            if self._prepare_write is not None:
                await self._prepare_write()          # e.g. apply scope-specific embedding
            written = await self._write_manager.add_memories(
                self._user_id, self._scope_id, memories, self._llm
            )

        count = len(written) if written is not None else 0
        memory_logger.info(
            "dreaming: promoted %s/%s knowledge items",
            count,
            attempted,
            event_type=LogEventType.MEMORY_STORE,
            user_id=self._user_id,
            scope_id=self._scope_id,
        )
        return count
