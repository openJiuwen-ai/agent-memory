# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Any

from pydantic import BaseModel, ConfigDict, Field

from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.store.base_memory_index import BaseMemoryIndex
from jiuwen_memory.foundation.store.filter_dsl import FilterGroup
from jiuwen_memory.memory_core.common.distributed_lock import DistributedLock
from jiuwen_memory.memory_core.manage.index.base_memory_manager import BaseMemoryManager
from jiuwen_memory.memory_core.manage.index.summary_manager import SummaryManager
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager, FRAGMENT_MEMORY_TYPE
from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType
from jiuwen_memory.memory_core.manage.search.filter_normalizer import (
    ensure_blacklisted_ne,
    normalize_filters,
)
from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType


# retrieve_history KV key prefix. Layout:
#   retrieve_history{SEP}{user_id}{SEP}{scope_id}{SEP}{mem_id}
# where SEP is ``VariableManager.SEPARATOR`` (currently ``/``).
RETRIEVE_HISTORY_PREFIX = "retrieve_history"

# FIFO rolling-window cap for ``retrieve_history`` list. Once the list
# reaches this length, the oldest (front) entry is dropped before the
# new ts is appended — preserves the most recent retrieve timestamps,
# which is what the Ebbinghaus reinforcement term (σ · Σ 1/d) cares about.
_RETRIEVE_HISTORY_MAX_LEN = 20


class SearchParams(BaseModel):
    user_id: str
    scope_id: str
    query: str
    top_k: int = Field(default=5, description="返回的最大结果数")
    threshold: float = Field(default=0.3, description="匹配阈值")
    search_type: Optional[list[str]] = Field(default=None, description="搜索类型")
    # Optional caller-supplied FilterGroup. None = auto-inject
    # ``NE("blacklisted", True)`` (exclude forgotten memories) via
    # ``ensure_blacklisted_ne``. Callers wanting to include blacklisted
    # memories should pass a FilterGroup that mentions the ``blacklisted``
    # field (e.g. ``EQ("blacklisted", True)``) — the framework then keeps
    # the caller's filters as-is.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    filters: Optional[FilterGroup] = Field(default=None)


class SearchManager:
    all_mem_manager_list = [item.value for item in MemoryType]

    def __init__(self,
                 managers: dict[str, BaseMemoryManager],
                 crypto_key: bytes,
                 memory_index: BaseMemoryIndex,
                 *,
                 kv_store: Optional[BaseKVStore] = None,
                 ):
        self.managers = managers
        self.crypto_key = crypto_key
        self.memory_index = memory_index
        # Optional KV store reference used by ``_append_retrieve_history``
        # to record search hits for the Ebbinghaus reinforcement term.
        # None = skip retrieve_history writes (backward compat for tests
        # that construct SearchManager without a kv_store).
        self.kv_store = kv_store

    @staticmethod
    def _retrieve_history_key(user_id: str, scope_id: str, mem_id: str) -> str:
        """retrieve_history KV key. Layout described at module top."""
        sep = VariableManager.SEPARATOR
        return f"{RETRIEVE_HISTORY_PREFIX}{sep}{user_id}{sep}{scope_id}{sep}{mem_id}"

    async def _append_retrieve_history(
        self,
        scope_id: str,
        user_id: str,
        mem_id: str,
        ts: datetime,
    ) -> None:
        """
        Fire-and-forget append of one retrieve timestamp for a mem_id.

        Called as a background ``asyncio.Task`` from ``search`` for each
        hit — never awaited from the search path, so the user-visible
        latency is unaffected. Failures (KV exception, lock contention,
        decode error) all downgrade to a ``memory_logger.warning`` and
        are swallowed; the next retrieve retry is the recovery path.

        The KV record layout is:
            Key:   retrieve_history{SEP}{user_id}{SEP}{scope_id}{SEP}{mem_id}
            Value: JSON ``{"retrieve_count": int,
                           "latest_retrieve_time": ISO 8601,
                           "retrieve_history": [ISO 8601, ...]}``

        Once ``retrieve_history`` reaches ``_RETRIEVE_HISTORY_MAX_LEN``
        (20) entries, the oldest entry (list[0]) is dropped before the
        new ts is appended — a FIFO rolling window that favours recency,
        which is what the Ebbinghaus reinforcement term (σ · Σ 1/d) cares
        about.
        """
        if not self.kv_store:
            # stores not registered — nothing we can do; emit a single
            # warning per call so we don't spam on every search hit
            # during a misconfigured test path.
            memory_logger.warning(
                "append retrieve_history skipped: kv_store is None",
                event_type=LogEventType.MEMORY_PROCESS,
                user_id=user_id,
                scope_id=scope_id,
                mem_id=mem_id,
            )
            return

        key = self._retrieve_history_key(user_id, scope_id, mem_id)
        lock = DistributedLock(self.kv_store, f"retrieve_history/{user_id}/{scope_id}/{mem_id}")
        try:
            async with lock:
                raw = await self.kv_store.get(key)
                if raw is None:
                    data: dict[str, Any] = {
                        "retrieve_count": 0,
                        "latest_retrieve_time": None,
                        "retrieve_history": [],
                    }
                else:
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="ignore")
                        data = json.loads(raw)
                    except (TypeError, ValueError):
                        # corrupt payload → start fresh; existing garbage
                        # is overwritten below. Log once so we can spot it.
                        memory_logger.warning(
                            "retrieve_history payload undecodable, resetting",
                            event_type=LogEventType.MEMORY_PROCESS,
                            user_id=user_id,
                            scope_id=scope_id,
                            mem_id=mem_id,
                        )
                        data = {
                            "retrieve_count": 0,
                            "latest_retrieve_time": None,
                            "retrieve_history": [],
                        }
                iso_ts = ts.isoformat()
                data["retrieve_count"] = int(data.get("retrieve_count", 0) or 0) + 1
                data["latest_retrieve_time"] = iso_ts
                history: list = list(data.get("retrieve_history") or [])
                if len(history) >= _RETRIEVE_HISTORY_MAX_LEN:
                    # FIFO: drop the oldest entry before appending the
                    # new ts. Keeps the list bounded at 20 entries and
                    # preserves the most recent retrieve timestamps —
                    # which is what Ebbinghaus reinforcement needs.
                    history.pop(0)
                history.append(iso_ts)
                data["retrieve_history"] = history
                await self.kv_store.set(key, json.dumps(data))
        except Exception as exc:
            # Lock acquisition failure / KV exception / network error —
            # we cannot recover inside a fire-and-forget task; log and
            # swallow. The next retrieve will try again.
            memory_logger.warning(
                "append retrieve_history failed, skip: %s",
                str(exc),
                event_type=LogEventType.MEMORY_PROCESS,
                user_id=user_id,
                scope_id=scope_id,
                mem_id=mem_id,
            )

    async def search(self, params: SearchParams, **kwargs) -> list[dict[str, Any]] | None:
        user_id = params.user_id
        scope_id = params.scope_id
        query = params.query
        top_k = params.top_k
        threshold = params.threshold
        search_type = params.search_type
        kwargs['mem_types'] = search_type
        # Resolve filters: normalize (validates), then inject NE("blacklisted", True)
        # unless the caller's filters already mention the ``blacklisted`` field.
        # This keeps the default search path (filters=None) excluding blacklisted
        # memories. Callers wanting to include blacklisted memories pass a
        # FilterGroup that mentions ``blacklisted`` (e.g. EQ("blacklisted", True)).
        filters = ensure_blacklisted_ne(normalize_filters(params.filters))
        kwargs['filters'] = filters
        # search_type is illegal
        if search_type is not None:
            for st in search_type:
                if st not in self.all_mem_manager_list:
                    raise build_error(
                        StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                        memory_type=st,
                        error_msg=f"{st} is not a valid search type",
                    )
        # search_type is valid, but the corresponding manager has not been initialized
        used_types = {}
        if search_type is not None:
            for st in search_type:
                if st and not self.managers.get(st):
                    raise build_error(
                        StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                        memory_type=st,
                        error_msg=f"{st} memory manager not inited",
                    )
                manager = self.managers[st]
                if manager not in used_types:
                    used_types[manager] = []
                used_types[manager].append(st)
        result = []
        # search_type not specified, traverse available managers
        if search_type is None:
            for manager in set(self.managers.values()):
                res = await manager.search(user_id=user_id, scope_id=scope_id, query=query, top_k=top_k, **kwargs)
                if res is not None:
                    result.extend(res)
        # call the manager corresponding to search_type
        else:
            for manager, types in used_types.items():
                kwargs['mem_types'] = types
                res = await manager.search(user_id=user_id, scope_id=scope_id,
                                           query=query, top_k=top_k, **kwargs)
                if res:
                    result.extend(res)

        # sort and truncate multiple search_type results based on score
        if len(result) > top_k:
            result.sort(key=lambda item: item["score"], reverse=True)
        final_result = [item for item in result if item["score"] >= threshold][:top_k]

        # Fire-and-forget retrieve_history append for each hit.
        # We only record retrievals for long-term memories (user_profile /
        # semantic / episodic / summary); middle-term memories are excluded
        # because they are not subject to Ebbinghaus forgetting.
        # ``create_task`` without ``await`` means the search path returns
        # immediately; task exceptions are caught inside the helper and
        # downgraded to a warning, so they never bubble up here.
        if self.kv_store is not None:
            now_ts = datetime.now(timezone.utc).astimezone()
            for item in final_result:
                mt = item.get("mem_type")
                if mt == MemoryType.MIDDLE_TERM_MEMORY.value:
                    continue
                mem_id = item.get("id")
                if not mem_id:
                    continue
                asyncio.create_task(self._append_retrieve_history(scope_id, user_id, mem_id, now_ts))

        return final_result

    async def search_middle(self, params: SearchParams, semantic_store, **kwargs) -> list[dict[str, Any]] | None:
        user_id = params.user_id
        scope_id = params.scope_id
        query = params.query
        top_k = params.top_k
        threshold = params.threshold
        search_type = params.search_type
        kwargs['mem_type'] = search_type
        # search_type is illegal
        if search_type is not None and search_type not in self.all_mem_manager_list:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=search_type,
                error_msg=f"{search_type} is not a valid search type",
            )
        # search_type is valid, but the corresponding manager has not been initialized
        if search_type and not self.managers.get(search_type):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=search_type,
                error_msg=f"{search_type} memory manager not inited",
            )
        result = []
        if search_type is None:
            for manager in set(self.managers.values()):
                res = await manager.search(user_id=user_id, scope_id=scope_id, query=query, top_k=top_k,
                                           semantic_store=semantic_store, **kwargs)
                if res is not None:
                    result.extend(res)
        # call the manager corresponding to search_type
        else:
            res = await self.managers[search_type].search(user_id=user_id, scope_id=scope_id, query=query, top_k=top_k,
                                                          semantic_store=semantic_store, **kwargs)
            if res:
                result = res

        result = [item for item in result if item[1] >= threshold]

        # sort and truncate multiple search_type results based on score
        if len(result) > top_k:
            result.sort(key=lambda item: item[1], reverse=True)

        return result[:top_k]

    async def list_user_mem(
            self,
            user_id: str,
            scope_id: str,
            nums: int,
            pages: int,
            mem_type: str = None,
            *,
            filters: Optional[FilterGroup] = None,
    ) -> list[dict[str, Any]] | None:
        """List user memories with pagination.

        Args:
            filters: caller-supplied FilterGroup. None = auto-inject
                ``NE("blacklisted", True)`` to exclude blacklisted memories.
                Callers wanting to include blacklisted memories pass a
                FilterGroup that mentions the ``blacklisted`` field (e.g.
                ``EQ("blacklisted", True)``) — the framework keeps such
                filters as-is.
        """
        result = []
        start = nums * (pages - 1)
        if not self.memory_index:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="search_memory",
                error_msg=f"memory index not inited",
            )
        # Default: exclude blacklisted. Callers wanting blacklisted memories
        # pass a FilterGroup mentioning the blacklisted field.
        filters = ensure_blacklisted_ne(normalize_filters(filters))
        if mem_type:
            result = await self.memory_index.list_memories(
                user_id, scope_id, start, nums, [mem_type], filters=filters,
            )
        else:
            result = await self.memory_index.list_memories(
                user_id, scope_id, start, nums, [], filters=filters,
            )
        result = [{
            "id": res.id,
            "user_id": user_id,
            "scope_id": scope_id,
            "mem": res.text,
            "mem_type": res.type,
            "timestamp": res.timestamp,
            # Surface the forget-pipeline flags as top-level keys so
            # callers (server endpoints, e2e tests) can inspect them
            # without re-fetching each doc via ``get_by_id``. The pushdown
            # path reads these off the vector scalar schema; the KV scan
            # path reads them off the KV JSON — both populate the
            # MemoryDoc attributes (see _kv_data_to_memory_doc).
            "blacklisted": res.blacklisted,
            "is_important": res.is_important,
            **res.fields,
        } for res in result]
        return result

    async def list_user_mem_with_total(
            self,
            user_id: str,
            scope_id: str,
            nums: int,
            pages: int,
            mem_type: str = None,
            *,
            filters: Optional[FilterGroup] = None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        """Like ``list_user_mem`` but also returns the global total count
        (before pagination). Always uses the KV scan path for correct total.
        """
        start = nums * (pages - 1)
        if not self.memory_index:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="search_memory",
                error_msg=f"memory index not inited",
            )
        filters = ensure_blacklisted_ne(normalize_filters(filters))
        if mem_type:
            result, total = await self.memory_index.list_memories_with_total(
                user_id, scope_id, start, nums, [mem_type], filters=filters,
            )
        else:
            result, total = await self.memory_index.list_memories_with_total(
                user_id, scope_id, start, nums, [], filters=filters,
            )
        result = [{
            "id": res.id,
            "user_id": user_id,
            "scope_id": scope_id,
            "mem": res.text,
            "mem_type": res.type,
            "timestamp": res.timestamp,
            "blacklisted": res.blacklisted,
            "is_important": res.is_important,
            **res.fields,
        } for res in result]
        return result, total

    async def list_user_profile(self, user_id: str, scope_id: str) -> list[dict]:
        if any(item not in self.managers for item in FRAGMENT_MEMORY_TYPE):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="fragment_memory",
                error_msg=f"fragment memory manager not inited",
            )
        if not isinstance(self.managers[MemoryType.USER_PROFILE.value], FragmentMemoryManager):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type="fragment_memory",
                error_msg=f"fragment memory manager class is not FragmentMemoryManager",
            )
        return await self.managers[MemoryType.USER_PROFILE.value].list_fragment_memories(
            user_id=user_id,
            scope_id=scope_id
        )

    async def list_user_summary(self, user_id: str, scope_id: str) -> list[dict]:
        if MemoryType.SUMMARY.value not in self.managers:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.SUMMARY.value,
                error_msg=f"{MemoryType.SUMMARY.value} memory manager not inited",
            )
        if not isinstance(self.managers[MemoryType.SUMMARY.value], SummaryManager):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.SUMMARY.value,
                error_msg=f"{MemoryType.SUMMARY.value} manager class is not SummaryManager",
            )
        return await self.managers[MemoryType.SUMMARY.value].list_user_summary(user_id=user_id,
                                                                               scope_id=scope_id)

    async def get_user_variable(self, user_id: str, scope_id: str, var_name: str) -> str | None:
        if MemoryType.VARIABLE.value not in self.managers:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.VARIABLE.value,
                error_msg=f"{MemoryType.VARIABLE.value} memory manager not inited",
            )
        if not isinstance(self.managers[MemoryType.VARIABLE.value], VariableManager):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.VARIABLE.value,
                error_msg=f"{MemoryType.VARIABLE.value} manager class is not VariableManager",
            )
        res = await self.managers[MemoryType.VARIABLE.value].query_variable(user_id=user_id,
                                                                            scope_id=scope_id, name=var_name)
        if res is None:
            return None
        return res[var_name] if var_name in res else None

    async def get_all_user_variable(self, user_id: str, scope_id: str) -> dict[str, Any]:
        if MemoryType.VARIABLE.value not in self.managers:
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.VARIABLE.value,
                error_msg=f"{MemoryType.VARIABLE.value} memory manager not inited",
            )
        if not isinstance(self.managers[MemoryType.VARIABLE.value], VariableManager):
            raise build_error(
                StatusCode.MEMORY_GET_MEMORY_EXECUTION_ERROR,
                memory_type=MemoryType.VARIABLE.value,
                error_msg=f"{MemoryType.VARIABLE.value} manager class is not VariableManager",
            )
        return await self.managers[MemoryType.VARIABLE.value].query_variable(user_id=user_id, scope_id=scope_id)
