# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from jiuwen_memory.memory_core.manage.index.base_memory_manager import BaseMemoryManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import BaseMemoryUnit, MemoryType
from jiuwen_memory.foundation.llm import Model
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


class WriteManager:
    def __init__(
            self,
            managers: dict[str, BaseMemoryManager],
            memory_index: BaseMemoryIndex,
            middle_manager: BaseMemoryManager | None = None,
            enable_middle_memory: bool = False,
            message_manager=None,
    ):
        self.managers = managers
        self.memory_index = memory_index
        self.middle_manager = middle_manager
        self._enable_middle_memory = enable_middle_memory
        self._message_manager = message_manager

    async def add_memories(self, user_id: str, scope_id: str, memories: dict[str, list[BaseMemoryUnit]],
                           llm: Model | None, **kwargs) -> list[BaseMemoryUnit]:
        if not memories:
            memory_logger.debug("No memory units to add", event_type=LogEventType.MEMORY_STORE)
            return []
        result = []
        for manager in set(self.managers.values()):
            try:
                mem_units = await manager.add_memories(
                    user_id=user_id, scope_id=scope_id, memories=memories, llm=llm, **kwargs)
                result.extend(mem_units)
            except Exception as e:
                memory_logger.error(
                    "Failed to add mem",
                    exception=str(e),
                    memory_type=manager.mem_type,
                    event_type=LogEventType.MEMORY_STORE,
                )
                raise e
        return result

    async def _middle_mem_exists(self, mem_id: str) -> bool:
        """检查中期记忆中是否存在指定 mem_id。"""
        if self._message_manager is None:
            return False
        try:
            result = await self._message_manager.get_by_id(mem_id)
            return result is not None
        except Exception:
            return False

    async def update_mem_by_id(self, user_id: str, scope_id: str, mem_id: str, memory: str, **kwargs):
        if not mem_id:
            memory_logger.warning(
                "mem_id must not be empty or None",
                memory_id=[mem_id],
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id,
                scope_id=scope_id,
            )
            return False
        mem_type = await self.__get_mem_type_from_index(user_id, scope_id, mem_id)
        if mem_type is None:
            # 记忆在长期索引中不存在
            if self._enable_middle_memory and self.middle_manager:
                # 开关开启时，检查中期记忆中是否存在该 mem_id
                if not await self._middle_mem_exists(mem_id):
                    memory_logger.warning(
                        "Memory not found in long-term or middle-term, cannot update",
                        memory_id=[mem_id],
                        event_type=LogEventType.MEMORY_STORE,
                        user_id=user_id,
                        scope_id=scope_id,
                    )
                    return False
                memory_logger.info(
                    "Memory not found in long-term index, trying middle-term update",
                    memory_id=[mem_id],
                    event_type=LogEventType.MEMORY_STORE,
                    user_id=user_id,
                    scope_id=scope_id,
                )
                return await self.middle_manager.update(
                    user_id=user_id,
                    scope_id=scope_id,
                    mem_id=mem_id,
                    new_memory=memory,
                    **kwargs,
                )
            # 开关关闭或无中期 manager，记忆不存在
            memory_logger.warning(
                "Memory not found, cannot update",
                memory_id=[mem_id],
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id,
                scope_id=scope_id,
            )
            return False
        return await self.managers[mem_type].update(user_id=user_id, scope_id=scope_id, mem_id=mem_id,
                                                    new_memory=memory, **kwargs)

    async def delete_mem_by_id(self, user_id: str, scope_id: str, mem_id: str, **kwargs):
        if not mem_id:
            memory_logger.warning(
                "mem_id must not be empty or None",
                memory_id=[mem_id],
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id,
                scope_id=scope_id,
            )
            return False
        mem_type = await self.__get_mem_type_from_index(user_id, scope_id, mem_id)
        if mem_type is None:
            # 记忆在长期索引中不存在
            if self._enable_middle_memory and self.middle_manager:
                # 开关开启时，检查中期记忆中是否存在该 mem_id
                if not await self._middle_mem_exists(mem_id):
                    memory_logger.warning(
                        "Memory not found in long-term or middle-term, cannot delete",
                        memory_id=[mem_id],
                        event_type=LogEventType.MEMORY_STORE,
                        user_id=user_id,
                        scope_id=scope_id,
                    )
                    return False
                memory_logger.info(
                    "Memory not found in long-term index, trying middle-term deletion",
                    memory_id=[mem_id],
                    event_type=LogEventType.MEMORY_STORE,
                    user_id=user_id,
                    scope_id=scope_id,
                )
                return await self.middle_manager.delete(
                    user_id=user_id,
                    scope_id=scope_id,
                    mem_id=mem_id,
                    **kwargs,
                )
            # 开关关闭或无中期 manager，记忆不存在
            memory_logger.warning(
                "Memory not found",
                memory_id=[mem_id],
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id,
                scope_id=scope_id,
            )
            return False
        return await self.managers[mem_type].delete(user_id=user_id, scope_id=scope_id,
                                                    mem_id=mem_id, **kwargs)

    async def delete_mem_by_user_id(self, user_id: str, scope_id: str, **kwargs):
        for manager in set(self.managers.values()):
            await manager.delete_by_user_id(user_id=user_id, scope_id=scope_id, **kwargs)

    async def __get_mem_type_from_index(self, user_id: str, scope_id: str, mem_id: str) -> str | None:
        memory_doc = await self.memory_index.get_by_id(user_id, scope_id, mem_id)
        if memory_doc and memory_doc.type:
            mem_type = memory_doc.type
            if mem_type in self.managers:
                return mem_type
            memory_logger.warning(
                "Unsupported mem_type",
                memory_id=[mem_id],
                memory_type=mem_type,
                event_type=LogEventType.MEMORY_STORE,
                user_id=user_id,
                scope_id=scope_id
            )

        memory_logger.warning(
            "Nonexistent memory or memory type",
            memory_id=[mem_id],
            event_type=LogEventType.MEMORY_STORE,
            user_id=user_id,
            scope_id=scope_id,
        )
        return None
