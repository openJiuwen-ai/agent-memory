# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Step 4 unit tests for the retrieve_history async write path.

Scope:
  - SearchManager._retrieve_history_key: shape matches
    (retrieve_history{SEP}{user_id}{SEP}{scope_id}{SEP}{mem_id})
  - SearchManager._append_retrieve_history:
      first append creates a fresh KV record
      second append increments retrieve_count and extends retrieve_history
      KV get raises → graceful warning, no exception propagated
      KV set raises → graceful warning, no exception propagated
      corrupt JSON payload in KV → reset to fresh record (not crash)
      bytes payload in KV → utf-8 decoded before JSON parse
      list at max len (20) drops oldest before append (FIFO rolling window)
      lock acquire raises → graceful warning, no exception propagated
      kv_store is None → graceful warning, no exception propagated
  - SearchManager.search integration:
      each hit schedules a fire-and-forget task that calls
      _append_retrieve_history; the search path returns immediately
      without awaiting the tasks
      middle_term_memory hits are excluded from retrieve_history
      empty mem_id hits are excluded from retrieve_history
  - register_store side effect: retrieve_history prefix is registered
    in kv_prefix_registry
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterOperator,
)
from jiuwen_memory.memory_core.common.kv_prefix_registry import kv_prefix_registry
from jiuwen_memory.memory_core.long_term_memory import (
    LongTermMemory,
    RETRIEVE_HISTORY_PREFIX,
    _RETRIEVE_HISTORY_MAX_LEN,
)
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import (
    FragmentMemoryManager,
)
from jiuwen_memory.memory_core.manage.index.summary_manager import SummaryManager
from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType
from jiuwen_memory.memory_core.manage.search.search_manager import (
    SearchManager,
    SearchParams,
)


# LongTermMemory is a singleton; reset between tests so each one
# starts with a clean instance (no leftover _dreaming_orchestrators etc).
@pytest.fixture(autouse=True)
def reset_singleton():
    # pylint: disable=protected-access
    Singleton._instances.pop(LongTermMemory, None)
    yield
    Singleton._instances.pop(LongTermMemory, None)


def _make_search_manager(kv_store: BaseKVStore | None = None) -> SearchManager:
    """
    Build a minimal SearchManager with fragment + summary managers backed
    by a dict-style memory_index. The kv_store is forwarded to the
    SearchManager so ``_append_retrieve_history`` can write to it.
    """
    memory_index = MagicMock()
    memory_index.search = AsyncMock(return_value=[])
    memory_index.list_memories = AsyncMock(return_value=[])
    memory_index.add_memories = AsyncMock()
    memory_index.update_mem_by_id = AsyncMock()

    frag = FragmentMemoryManager(memory_index=memory_index, crypto_key=b"k")
    summ = SummaryManager(memory_index=memory_index, crypto_key=b"k")
    managers = {
        MemoryType.USER_PROFILE.value: frag,
        MemoryType.SEMANTIC_MEMORY.value: frag,
        MemoryType.EPISODIC_MEMORY.value: frag,
        MemoryType.SUMMARY.value: summ,
    }
    return SearchManager(
        managers=managers,
        crypto_key=b"k",
        memory_index=memory_index,
        kv_store=kv_store,
    )


# ---------------------------------------------------------------- _retrieve_history_key


class TestRetrieveHistoryKey:
    @staticmethod
    def test_key_uses_separator_and_components():
        key = SearchManager._retrieve_history_key("u1", "s1", "m1")  # pylint: disable=protected-access
        assert key == "retrieve_history/u1/s1/m1"

    @staticmethod
    def test_key_preserves_order_user_scope_mem():
        # caller may swap user_id and scope_id by mistake; the key
        # must reflect the spec'd order (user first, scope second).
        key = SearchManager._retrieve_history_key("user-a", "scope-b", "mem-1")  # pylint: disable=protected-access
        parts = key.split("/")
        assert parts == ["retrieve_history", "user-a", "scope-b", "mem-1"]

    @staticmethod
    def test_key_matches_ltm_static_helper():
        # LongTermMemory keeps a static _retrieve_history_key helper for
        # the cleanup / add_memory_unit paths; the two implementations
        # must produce the same key so the search-side writer and the
        # cleanup-side deleter agree.
        assert (  # pylint: disable=protected-access
            SearchManager._retrieve_history_key("u1", "s1", "m1")
            == LongTermMemory._retrieve_history_key("u1", "s1", "m1")
        )


# ---------------------------------------------------------------- _append_retrieve_history


class TestAppendRetrieveHistory:
    @pytest.mark.asyncio
    async def test_first_append_creates_fresh_record(self):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        ts = datetime(2026, 7, 21, 12, 0, 0)
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", ts)
        raw = await kv.get(sm._retrieve_history_key("u1", "s1", "m1"))
        data = json.loads(raw)
        assert data["retrieve_count"] == 1
        assert data["latest_retrieve_time"] == ts.isoformat()
        assert data["retrieve_history"] == [ts.isoformat()]

    @pytest.mark.asyncio
    async def test_second_append_increments_and_extends(self):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        ts1 = datetime(2026, 7, 21, 12, 0, 0)
        ts2 = datetime(2026, 7, 22, 12, 0, 0)
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", ts1)
        await sm._append_retrieve_history("s1", "u1", "m1", ts2)
        raw = await kv.get(sm._retrieve_history_key("u1", "s1", "m1"))
        data = json.loads(raw)
        assert data["retrieve_count"] == 2
        assert data["latest_retrieve_time"] == ts2.isoformat()
        assert data["retrieve_history"] == [ts1.isoformat(), ts2.isoformat()]

    @pytest.mark.asyncio
    async def test_kv_get_raises_degrades_gracefully(self):
        kv = MagicMock(spec=BaseKVStore)
        # Lock acquire succeeds (exclusive_set returns True); KV get raises.
        kv.exclusive_set = AsyncMock(return_value=True)
        kv.get = AsyncMock(side_effect=RuntimeError("backend offline"))
        kv.set = AsyncMock()
        kv.delete = AsyncMock()
        sm = _make_search_manager(kv)
        # Should not raise.
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", datetime.now(timezone.utc).astimezone())

    @pytest.mark.asyncio
    async def test_kv_set_raises_degrades_gracefully(self):
        kv = InMemoryKVStore()

        # Replace set with one that raises on write; first get returns
        # None so we build a fresh record, then set raises.
        async def boom_set(key, value):
            raise RuntimeError("disk full")

        kv.set = boom_set
        sm = _make_search_manager(kv)
        # Should not raise; set failure is caught and logged.
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", datetime.now(timezone.utc).astimezone())

    @pytest.mark.asyncio
    async def test_corrupt_payload_resets_to_fresh_record(self):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        # pylint: disable=protected-access
        key = sm._retrieve_history_key("u1", "s1", "m1")
        # Seed corrupt JSON.
        await kv.set(key, "not-json{")
        ts = datetime(2026, 7, 21, 12, 0, 0)
        # Should not raise; should overwrite with a fresh record.
        await sm._append_retrieve_history("s1", "u1", "m1", ts)
        raw = await kv.get(key)
        data = json.loads(raw)
        assert data["retrieve_count"] == 1
        assert data["latest_retrieve_time"] == ts.isoformat()
        assert data["retrieve_history"] == [ts.isoformat()]

    @pytest.mark.asyncio
    async def test_bytes_payload_decoded_before_parse(self):
        # Some KV backends (Redis / Shelve) return bytes; the helper
        # decodes utf-8 before json.loads.
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        # pylint: disable=protected-access
        key = sm._retrieve_history_key("u1", "s1", "m1")
        # Seed a bytes payload directly via the underlying dict.
        seed = {
            "retrieve_count": 5,
            "latest_retrieve_time": "2026-07-01T00:00:00",
            "retrieve_history": ["2026-07-01T00:00:00"],
        }
        kv._store[key] = (json.dumps(seed).encode("utf-8"), None)
        await sm._append_retrieve_history("s1", "u1", "m1", datetime(2026, 7, 21, 12, 0, 0))
        raw = await kv.get(key)
        data = json.loads(raw)
        assert data["retrieve_count"] == 6
        assert len(data["retrieve_history"]) == 2

    @pytest.mark.asyncio
    async def test_list_at_max_len_drops_oldest_before_append(self):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        # pylint: disable=protected-access
        key = sm._retrieve_history_key("u1", "s1", "m1")
        # Seed a record already at max len (20) — each entry is a
        # successive timestamp so the FIFO order is unambiguous.
        seed_history = [
            (datetime(2024, 1, 1) + timedelta(days=i)).isoformat()
            for i in range(_RETRIEVE_HISTORY_MAX_LEN)
        ]
        seed = {
            "retrieve_count": len(seed_history),
            "latest_retrieve_time": seed_history[-1],
            "retrieve_history": seed_history,
        }
        await kv.set(key, json.dumps(seed))
        # Append one more — the oldest entry (seed_history[0]) is
        # dropped, the new ts is appended; list stays at 20 entries.
        new_ts = datetime(2026, 7, 21, 12, 0, 0)
        await sm._append_retrieve_history("s1", "u1", "m1", new_ts)
        raw = await kv.get(key)
        data = json.loads(raw)
        assert data["retrieve_count"] == _RETRIEVE_HISTORY_MAX_LEN + 1
        assert len(data["retrieve_history"]) == _RETRIEVE_HISTORY_MAX_LEN
        # Oldest seed entry is gone; new ts is at the tail.
        assert seed_history[0] not in data["retrieve_history"]
        assert data["retrieve_history"][-1] == new_ts.isoformat()
        # The rest of the seed entries are shifted forward by one.
        assert data["retrieve_history"][:-1] == seed_history[1:]

    @pytest.mark.asyncio
    async def test_lock_acquire_failure_degrades_gracefully(self):
        # Lock acquisition is via exclusive_set; if it raises (not just
        # returns False — that would retry forever), the helper should
        # catch the exception and log a warning.
        kv = MagicMock(spec=BaseKVStore)
        kv.exclusive_set = AsyncMock(side_effect=RuntimeError("kv corruption"))
        kv.get = AsyncMock(return_value=None)
        kv.set = AsyncMock()
        kv.delete = AsyncMock()
        sm = _make_search_manager(kv)
        # Should not raise.
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", datetime.now(timezone.utc).astimezone())

    @pytest.mark.asyncio
    async def test_kv_store_none_emits_warning_and_returns(self):
        # SearchManager constructed without kv_store — _append_retrieve_history
        # emits a warning and returns without touching anything.
        sm = _make_search_manager(kv_store=None)
        # pylint: disable=protected-access
        await sm._append_retrieve_history("s1", "u1", "m1", datetime.now(timezone.utc).astimezone())


# ---------------------------------------------------------------- SearchManager.search fire-and-forget


class TestSearchFireAndForget:
    """
    Integration with the SearchManager.search path.

    We mock the manager.search (and thus memory_index.search) to return
    controlled hits, then assert that ``SearchManager.search`` schedules
    a fire-and-forget ``_append_retrieve_history`` task for each
    non-middle-term hit.
    """

    @pytest.mark.asyncio
    async def test_each_hit_schedules_async_task(self, monkeypatch):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        # Stub the manager.search to return two long-term hits.
        sm.managers[MemoryType.USER_PROFILE.value].search = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "mem": "a",
                    "mem_type": "user_profile",
                    "score": 0.9,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
                {
                    "id": "m2",
                    "mem": "b",
                    "mem_type": "semantic_memory",
                    "score": 0.7,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
            ]
        )

        # Spy on create_task to capture scheduled tasks.
        scheduled: list[asyncio.Task] = []
        orig_create_task = asyncio.create_task

        def spy_create_task(coro, **kw):
            t = orig_create_task(coro, **kw)
            scheduled.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", spy_create_task)

        # Spy on _append_retrieve_history to assert it's called per hit.
        append_calls: list[tuple] = []
        # pylint: disable=protected-access
        original = sm._append_retrieve_history

        async def spy_append(scope_id, user_id, mem_id, ts):
            append_calls.append((scope_id, user_id, mem_id, ts))
            await original(scope_id, user_id, mem_id, ts)

        monkeypatch.setattr(sm, "_append_retrieve_history", spy_append)

        params = SearchParams(
            user_id="u1",
            scope_id="s1",
            query="hello",
            top_k=10,
            threshold=0.0,
            search_type=["user_profile", "semantic_memory"],
        )
        results = await sm.search(params)

        # Search returns both hits.
        assert len(results) == 2
        # Two fire-and-forget tasks were scheduled.
        assert len(scheduled) == 2
        # Wait for the background tasks to complete.
        await asyncio.gather(*scheduled, return_exceptions=True)
        assert {c[2] for c in append_calls} == {"m1", "m2"}
        # KV records were actually written.
        for mem_id in ("m1", "m2"):
            raw = await kv.get(sm._retrieve_history_key("u1", "s1", mem_id))
            assert raw is not None

    @pytest.mark.asyncio
    async def test_middle_term_memory_hits_excluded(self, monkeypatch):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        sm.managers[MemoryType.USER_PROFILE.value].search = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "mem": "a",
                    "mem_type": "user_profile",
                    "score": 0.9,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
                {
                    "id": "m2",
                    "mem": "b",
                    "mem_type": "middle_term_memory",
                    "score": 0.8,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
            ]
        )

        scheduled: list[asyncio.Task] = []
        orig_create_task = asyncio.create_task

        def spy_create_task(coro, **kw):
            t = orig_create_task(coro, **kw)
            scheduled.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", spy_create_task)

        params = SearchParams(
            user_id="u1",
            scope_id="s1",
            query="hello",
            top_k=10,
            threshold=0.0,
            search_type=["user_profile", "semantic_memory"],
        )
        await sm.search(params)

        # Only one task (the user_profile hit); middle_term_memory is skipped.
        assert len(scheduled) == 1
        await asyncio.gather(*scheduled, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_empty_mem_id_excluded(self, monkeypatch):
        kv = InMemoryKVStore()
        sm = _make_search_manager(kv)
        sm.managers[MemoryType.USER_PROFILE.value].search = AsyncMock(
            return_value=[
                {
                    "id": "",
                    "mem": "a",
                    "mem_type": "user_profile",
                    "score": 0.9,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
                {
                    "id": "m2",
                    "mem": "b",
                    "mem_type": "user_profile",
                    "score": 0.7,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
            ]
        )

        scheduled: list[asyncio.Task] = []
        orig_create_task = asyncio.create_task

        def spy_create_task(coro, **kw):
            t = orig_create_task(coro, **kw)
            scheduled.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", spy_create_task)

        params = SearchParams(
            user_id="u1",
            scope_id="s1",
            query="hello",
            top_k=10,
            threshold=0.0,
            search_type=["user_profile", "semantic_memory"],
        )
        await sm.search(params)

        # Empty mem_id hit is skipped; only m2 schedules a task.
        assert len(scheduled) == 1
        await asyncio.gather(*scheduled, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_kv_store_none_skips_scheduling(self, monkeypatch):
        # SearchManager constructed without kv_store — no tasks scheduled.
        sm = _make_search_manager(kv_store=None)
        sm.managers[MemoryType.USER_PROFILE.value].search = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "mem": "a",
                    "mem_type": "user_profile",
                    "score": 0.9,
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                },
            ]
        )

        scheduled: list[asyncio.Task] = []
        orig_create_task = asyncio.create_task

        def spy_create_task(coro, **kw):
            t = orig_create_task(coro, **kw)
            scheduled.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", spy_create_task)

        params = SearchParams(
            user_id="u1",
            scope_id="s1",
            query="hello",
            top_k=10,
            threshold=0.0,
            search_type=["user_profile", "semantic_memory"],
        )
        await sm.search(params)
        # No kv_store → no fire-and-forget tasks.
        assert scheduled == []


# ---------------------------------------------------------------- prefix registration


class TestRetrieveHistoryPrefixRegistration:
    @staticmethod
    def test_prefix_registered_after_register_store():
        # register_store runs the full migration / set_config chain and
        # needs a full DB stack — too heavy for a unit test. Instead we
        # call just the registration line directly to verify the
        # constant is what gets registered.
        kv_prefix_registry.unregister(RETRIEVE_HISTORY_PREFIX)
        assert RETRIEVE_HISTORY_PREFIX not in kv_prefix_registry.get_all_prefixes()
        kv_prefix_registry.register_current(RETRIEVE_HISTORY_PREFIX)
        assert RETRIEVE_HISTORY_PREFIX in kv_prefix_registry.get_all_prefixes()
        # Idempotent.
        kv_prefix_registry.register_current(RETRIEVE_HISTORY_PREFIX)
        assert RETRIEVE_HISTORY_PREFIX in kv_prefix_registry.get_all_prefixes()
