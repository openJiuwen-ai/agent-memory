# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from datetime import datetime, timezone
from typing import Any, List, Tuple

from jiuwen_memory.foundation.llm import Model
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex, MemoryDoc
from jiuwen_memory.memory_core.manage.index.base_memory_manager import BaseMemoryManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import SummaryUnit, BaseMemoryUnit, MemoryType
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


class SummaryManager(BaseMemoryManager):

    def __init__(self,
                 memory_index: BaseMemoryIndex,
                 crypto_key: bytes = None):
        self.memory_index = memory_index
        self.crypto_key = crypto_key
        self.mem_type = MemoryType.SUMMARY.value

    @staticmethod
    def _parse_timestamp(ts: str) -> datetime:
        if isinstance(ts, datetime):
            return ts
        if not ts:
            return datetime.now(timezone.utc).astimezone()
        for fmt in ("%Y-%m-%d %H-%M-%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt).replace(
                    tzinfo=datetime.now(timezone.utc).astimezone().tzinfo)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
        return datetime.now(timezone.utc).astimezone()

    def _convert_to_memory_docs(self, memories: dict[str, list[BaseMemoryUnit]]) -> List[MemoryDoc]:
        memory_docs = []
        for mem_type, memory_list in memories.items():
            if mem_type != self.mem_type:
                continue
            for mem_unit in memory_list:
                if not isinstance(mem_unit, SummaryUnit):
                    continue
                # 空摘要不入库：避免向量化时触发 embedding 的 "empty chunk" 校验报错
                if not mem_unit.summary or not mem_unit.summary.strip():
                    continue

                memory_doc = MemoryDoc(
                    id=mem_unit.mem_id,
                    text=mem_unit.summary,
                    type=mem_type,
                    timestamp=self._parse_timestamp(mem_unit.timestamp),
                    fields={
                        "source_id": mem_unit.message_mem_id,
                        "metadata": {}
                    },
                    is_important=mem_unit.is_important,
                )
                memory_docs.append(memory_doc)
        return memory_docs

    async def add_memories(self, user_id: str, scope_id: str, memories: dict[str, list[BaseMemoryUnit]],
                           llm: Tuple[str, Model] | None = None, **kwargs):
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            memory_docs = self._convert_to_memory_docs(memories)
            if not memory_docs:
                memory_logger.warning(
                    "No valid summary docs to add",
                    event_type=LogEventType.MEMORY_STORE,
                    memory_type=self.mem_type,
                    user_id=user_id,
                    scope_id=scope_id
                )
                return []
            await self.memory_index.add_memories(user_id, scope_id, memory_docs)
            return memories[self.mem_type]
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def update(self, user_id: str, scope_id: str, mem_id: str, new_memory: str, **kwargs):
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            memory_doc = await self.memory_index.get_by_id(user_id, scope_id, mem_id)
            if not memory_doc:
                return False
            updated_doc = MemoryDoc(
                id=mem_id,
                text=new_memory,
                type=self.mem_type,
                timestamp=datetime.now(timezone.utc).astimezone(),
                fields=memory_doc.fields,
                # Preserve forgetting-related flags from the existing doc:
                # update_mem_by_id only rewrites content (and re-embeds);
                # the Ebbinghaus tags must survive the rewrite.
                is_important=memory_doc.is_important,
                blacklisted=memory_doc.blacklisted,
            )
            await self.memory_index.update_memories(user_id, scope_id, [updated_doc])
            return True
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_UPDATE_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def delete(self, user_id: str, scope_id: str, mem_id: str, **kwargs):
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            await self.memory_index.delete_memories(user_id, scope_id, [mem_id])
            return True
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def delete_by_user_id(self, user_id: str, scope_id: str, **kwargs):
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            await self.memory_index.delete_by_user_and_scope(user_id, scope_id)
            return True
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_DELETE_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def get(self, user_id: str, scope_id: str, mem_id: str) -> dict[str, Any] | None:
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            memory_doc = await self.memory_index.get_by_id(user_id, scope_id, mem_id)
            if not memory_doc:
                return None

            return {
                "id": memory_doc.id,
                "mem": memory_doc.text,
                "mem_type": memory_doc.type,
                "timestamp": memory_doc.timestamp,
                "source_id": memory_doc.fields.get("source_id"),
                "metadata": memory_doc.fields.get("metadata")
            }
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def search(self, user_id: str, scope_id: str, query: str, top_k: int, **kwargs):
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            # Optional FilterGroup forwarded by the search entrypoint.
            filters = kwargs.get("filters", None)
            search_results = await self.memory_index.search(
                user_id=user_id,
                scope_id=scope_id,
                query=query,
                mem_types=[self.mem_type],
                top_k=top_k,
                filters=filters,
            )

            result = []
            for memory_doc, score in search_results:

                result.append({
                    "id": memory_doc.id,
                    "mem": memory_doc.text,
                    "mem_type": memory_doc.type,
                    "timestamp": memory_doc.timestamp,
                    "score": score,
                    "source_id": memory_doc.fields.get("source_id"),
                    "metadata": memory_doc.fields.get("metadata")
                })
            return result
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type)

    async def list_user_summary(self, user_id: str, scope_id: str,
                                offset: int = 0, batch_size: int = 100) -> list[dict[str, Any]]:
        self._validate_required_params(
            user_id, scope_id, self.memory_index,
            StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type,
        )

        try:
            summary_memories = await self.memory_index.list_memories(user_id, scope_id,
                                                        offset, batch_size, [self.mem_type])
            if not summary_memories:
                return []
            result = []
            for memory_doc in summary_memories:

                result.append({
                    "id": memory_doc.id,
                    "mem": memory_doc.text,
                    "mem_type": memory_doc.type,
                    "timestamp": memory_doc.timestamp,
                    "source_id": memory_doc.fields.get("source_id"),
                    "metadata": memory_doc.fields.get("metadata")
                })

            result.sort(key=lambda x: x['timestamp'], reverse=True)
            return result
        except BaseError:
            raise
        except Exception as e:
            self._wrap_exception(e, StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR, self.mem_type)
