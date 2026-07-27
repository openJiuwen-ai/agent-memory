# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# pylint: disable=protected-access
"""
Step 8 unit tests — ``delete_mem_by_id`` cleans up Ebbinghaus forgetting
traces (retrieve_history KV key), and the update path on fragment /
summary managers preserves ``is_important`` / ``blacklisted``.

Coverage:
  - delete_mem_by_id happy path: write_manager.delete_mem_by_id called,
    then retrieve_history KV delete called.
  - delete_mem_by_id KV cleanup failure does NOT break the main delete.
  - delete_mem_by_id retrieve_history missing → silent (kv.delete is best-effort).
  - cleanup_forgetting_traces when kv_store is None → no-op.

  (update field preservation already covered by Step 5d tests
  ``test_fragment_update_preserves_is_important_and_blacklisted`` and
  ``test_summary_update_preserves_is_important_and_blacklisted`` — Step
  8a co-located the impl with Step 5c.)
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory


def _dict_kv_backed():
    backing: dict = {}

    async def _exclusive_set(key, value, expiry=None):
        if key in backing:
            return False
        backing[key] = value
        return True

    async def _get(key):
        return backing.get(key)

    async def _delete(key):
        backing.pop(key, None)

    async def _set(key, value):
        backing[key] = value

    async def _delete_by_prefix(prefix, batch_size=None):
        # Walk a snapshot of keys; prefix-match like a real KV would.
        for key in list(backing.keys()):
            if key.startswith(prefix):
                backing.pop(key, None)

    kv = MagicMock()
    kv.exclusive_set = AsyncMock(side_effect=_exclusive_set)
    kv.get = AsyncMock(side_effect=_get)
    kv.set = AsyncMock(side_effect=_set)
    kv.delete = AsyncMock(side_effect=_delete)
    kv.delete_by_prefix = AsyncMock(side_effect=_delete_by_prefix)
    return kv, backing


@pytest.fixture
def ltm():
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()
    kv, _ = _dict_kv_backed()
    m.kv_store = kv
    m.write_manager = MagicMock()
    m.write_manager.delete_mem_by_id = AsyncMock()
    m.write_manager.delete_mem_by_user_id = AsyncMock()
    yield m
    Singleton._instances.pop(LongTermMemory, None)


def _rh_key(user_id, scope_id, mem_id):
    from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
    return (
        f"retrieve_history{VariableManager.SEPARATOR}{user_id}"
        f"{VariableManager.SEPARATOR}{scope_id}"
        f"{VariableManager.SEPARATOR}{mem_id}"
    )


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_delete_mem_by_id_calls_write_manager_and_cleans_kv(ltm):
    # Seed KV: retrieve_history contains mem_id "m1"
    rh_key = _rh_key("u", "s", "m1")
    await ltm.kv_store.set(rh_key, json.dumps({
        "retrieve_count": 3, "latest_retrieve_time": "x", "retrieve_history": ["a", "b"],
    }))

    await ltm.delete_mem_by_id(mem_id="m1", user_id="u", scope_id="s")

    # write_manager.delete_mem_by_id called
    ltm.write_manager.delete_mem_by_id.assert_awaited_once()
    # retrieve_history key is gone
    assert await ltm.kv_store.get(rh_key) is None


@pytest.mark.asyncio
async def test_delete_mem_by_id_missing_retrieve_history_is_silent(ltm):
    """
    If retrieve_history was never written, kv.delete is still called
    (it's a no-op on a missing key).
    """
    rh_key = _rh_key("u", "s", "m1")
    # Don't seed retrieve_history
    await ltm.delete_mem_by_id(mem_id="m1", user_id="u", scope_id="s")
    # The delete call was issued (best-effort); no exception raised
    ltm.kv_store.delete.assert_awaited()
    assert await ltm.kv_store.get(rh_key) is None


@pytest.mark.asyncio
async def test_delete_mem_by_id_kv_cleanup_failure_does_not_break_main_delete(ltm):
    """
    KV cleanup raises — main delete flow already succeeded; the caller
    should not see an exception.
    """
    ltm.kv_store.delete = AsyncMock(side_effect=RuntimeError("kv down"))
    # Main delete should succeed
    await ltm.delete_mem_by_id(mem_id="m1", user_id="u", scope_id="s")
    ltm.write_manager.delete_mem_by_id.assert_awaited_once()


# ---------------------------------------------------------------- kv_store None


@pytest.mark.asyncio
async def test_cleanup_forgetting_traces_kv_store_none_is_noop():
    """
    If kv_store is None (stores not registered), cleanup is a no-op —
    shouldn't raise.
    """
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()
    m.kv_store = None
    try:
        # Direct call to the helper — should not raise
        await m._cleanup_forgetting_traces(scope_id="s", user_id="u", mem_id="m1")
    finally:
        Singleton._instances.pop(LongTermMemory, None)


# ---------------------------------------------------------------- user-level lock


@pytest.mark.asyncio
async def test_delete_mem_by_id_acquires_user_lock(ltm):
    """
    delete_mem_by_id must hold the same user-level lock add_messages
    uses, so concurrent add/delete on the same user can't race.
    """
    await ltm.delete_mem_by_id(mem_id="m1", user_id="u", scope_id="s")
    lock_keys = [c.args[0] for c in ltm.kv_store.exclusive_set.call_args_list]
    assert "_lock/user/u" in lock_keys


# ---------------------------------------------------------------- batch delete cleanup
# ``delete_mem_by_user_id`` / ``delete_mem_by_scope`` must clean up the
# Ebbinghaus traces the same way ``delete_mem_by_id`` does — otherwise the
# retrieve_history/{user}/{scope}/{mem_id} keys become orphans and the
# blacklist/{scope}/{user} set keeps stale mem_ids forever ("forget me"
# compliance gap).


@pytest.mark.asyncio
async def test_delete_mem_by_user_id_drops_retrieve_history_prefix(ltm):
    """delete_mem_by_id must delete every retrieve_history key for
    that user across all scopes (prefix sweep).
    """
    # Seed retrieve_history under two scopes + an unrelated user that
    # must survive.
    await ltm.kv_store.set(_rh_key("u", "s1", "m1"), json.dumps({"retrieve_count": 1}))
    await ltm.kv_store.set(_rh_key("u", "s1", "m2"), json.dumps({"retrieve_count": 2}))
    await ltm.kv_store.set(_rh_key("u", "s2", "m3"), json.dumps({"retrieve_count": 1}))
    await ltm.kv_store.set(_rh_key("other", "s1", "mX"), json.dumps({"retrieve_count": 9}))

    await ltm.delete_mem_by_user_id(user_id="u", scope_id="s1")

    # All of u's retrieve_history keys are gone, regardless of scope.
    assert await ltm.kv_store.get(_rh_key("u", "s1", "m1")) is None
    assert await ltm.kv_store.get(_rh_key("u", "s1", "m2")) is None
    assert await ltm.kv_store.get(_rh_key("u", "s2", "m3")) is None
    # Unrelated user's key survives — the sweep is user-scoped.
    assert await ltm.kv_store.get(_rh_key("other", "s1", "mX")) is not None


@pytest.mark.asyncio
async def test_delete_mem_by_user_id_kv_cleanup_failure_does_not_break_main_delete(ltm):
    """KV prefix sweep raises — main delete flow already succeeded; the
    caller must not see an exception.
    """
    ltm.kv_store.delete_by_prefix = AsyncMock(side_effect=RuntimeError("kv down"))
    await ltm.delete_mem_by_user_id(user_id="u", scope_id="s")
    ltm.write_manager.delete_mem_by_user_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_forgetting_traces_batch_kv_store_none_is_noop():
    """kv_store is None (stores not registered) — cleanup is a no-op,
    must not raise. Batch path (mem_id omitted).
    """
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()
    m.kv_store = None
    try:
        await m._cleanup_forgetting_traces(scope_id="s", user_id="u")
    finally:
        Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
async def test_delete_mem_by_user_id_acquires_user_lock(ltm):
    """delete_mem_by_user_id must hold the same user-level lock the per-mem
    delete uses, so a concurrent add / batch-delete can't race.
    """
    await ltm.delete_mem_by_user_id(user_id="u", scope_id="s")
    lock_keys = [c.args[0] for c in ltm.kv_store.exclusive_set.call_args_list]
    assert "_lock/user/u" in lock_keys


@pytest.mark.asyncio
async def test_delete_mem_by_scope_runs_user_cleanup_per_user(ltm):
    """delete_mem_by_scope fans out per user_id via
    write_manager.delete_mem_by_user_id, then runs the user-level
    forgetting-traces cleanup inside the same per-user lock scope —
    so each user's retrieve_history keys are swept.
    """
    # scope_user_mapping_manager returns the user list that delete_mem_by_scope
    # iterates over.
    ltm.scope_user_mapping_manager = MagicMock()
    ltm.scope_user_mapping_manager.get_by_scope_id = AsyncMock(
        return_value=[{"user_id": "u1"}, {"user_id": "u2"}],
    )
    ltm.scope_user_mapping_manager.delete_by_scope_id = AsyncMock()

    # Seed retrieve_history for both users.
    await ltm.kv_store.set(_rh_key("u1", "s", "m1"), json.dumps({"retrieve_count": 1}))
    await ltm.kv_store.set(_rh_key("u2", "s", "m2"), json.dumps({"retrieve_count": 1}))

    ok = await ltm.delete_mem_by_scope(scope_id="s")
    assert ok is True

    # Both users' retrieve_history keys are gone.
    assert await ltm.kv_store.get(_rh_key("u1", "s", "m1")) is None
    assert await ltm.kv_store.get(_rh_key("u2", "s", "m2")) is None
