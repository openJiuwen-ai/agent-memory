# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Tests set the private prepare_write hook on the store under test.
# pylint: disable=protected-access

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory_core.common.distributed_lock import DistributedLock
from memory_core.process.dreaming.store import KnowledgeItem, MemoryUnitKnowledgeStore
from memory_core.manage.mem_model.memory_unit import FragmentMemoryUnit, MemoryType


def _make_store():
    write_manager = MagicMock()
    # write_manager.add_memories echoes back the units it was given (post-dedup == identity here)
    write_manager.add_memories = AsyncMock(
        side_effect=lambda u, s, mems, llm: [m for lst in mems.values() for m in lst])

    # dict-backed kv so DistributedLock acquire/release round-trips like the real store
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

    kv_store = MagicMock()
    kv_store.exclusive_set = AsyncMock(side_effect=_exclusive_set)
    kv_store.get = AsyncMock(side_effect=_get)
    kv_store.delete = AsyncMock(side_effect=_delete)
    store = MemoryUnitKnowledgeStore(write_manager, kv_store, llm="LLM", user_id="u", scope_id="s")
    return store, write_manager, kv_store


def _item(mem_type="semantic_memory", title="t", content="c", sid="sess_1"):
    return KnowledgeItem(mem_type=mem_type, title=title, content=content, source_session_id=sid)


@pytest.mark.asyncio
async def test_promote_builds_units_grouped_by_value():
    store, wm, _ = _make_store()
    n = await store.promote([
        _item("user_profile", "Job", "data analyst", "sess_a"),
        _item("semantic_memory", "Fact", "likes python", "sess_a"),
        _item("semantic_memory", "Fact2", "uses pandas", "sess_b"),
    ])

    assert n == 3
    args, _ = wm.add_memories.call_args
    user_id, scope_id, memories, llm = args
    assert (user_id, scope_id, llm) == ("u", "s", "LLM")
    # grouped by MemoryType.value (str keys)
    assert set(memories.keys()) == {"user_profile", "semantic_memory"}
    assert len(memories["semantic_memory"]) == 2
    unit = memories["user_profile"][0]
    assert isinstance(unit, FragmentMemoryUnit)
    assert unit.mem_type is MemoryType.USER_PROFILE          # enum, not str
    assert unit.content == "Job\ndata analyst"               # title folded in
    assert unit.message_mem_id == "sess_a"                   # provenance → source_id
    assert unit.mem_id                                       # generated id present


@pytest.mark.asyncio
async def test_invalid_mem_type_is_dropped():
    store, wm, _ = _make_store()
    n = await store.promote([
        _item("semantic_memory", "ok", "kept"),
        _item("garbage", "bad", "dropped"),
        _item("summary", "also-bad", "dropped"),   # valid enum member but not a fragment type
    ])

    assert n == 1
    _, _, memories, _ = wm.add_memories.call_args.args
    assert set(memories.keys()) == {"semantic_memory"}


@pytest.mark.asyncio
async def test_empty_input_skips_write_and_lock():
    store, wm, kv = _make_store()
    assert await store.promote([]) == 0
    wm.add_memories.assert_not_called()
    kv.exclusive_set.assert_not_called()


@pytest.mark.asyncio
async def test_all_invalid_skips_write():
    store, wm, kv = _make_store()
    assert await store.promote([_item("garbage"), _item("nope")]) == 0
    wm.add_memories.assert_not_called()
    kv.exclusive_set.assert_not_called()


@pytest.mark.asyncio
async def test_empty_text_item_is_dropped():
    store, wm, _ = _make_store()
    n = await store.promote([_item("semantic_memory", title="", content="")])
    assert n == 0
    wm.add_memories.assert_not_called()


@pytest.mark.asyncio
async def test_holds_user_lock_around_write():
    store, wm, kv = _make_store()
    await store.promote([_item("semantic_memory", "t", "c")])

    # the user lock key matches add_messages' lock: "_lock/" + "user/{user_id}"
    lock_args, _ = kv.exclusive_set.call_args
    assert lock_args[0] == "_lock/user/u"
    kv.exclusive_set.assert_awaited()   # acquired
    kv.delete.assert_awaited()          # released


@pytest.mark.asyncio
async def test_returns_post_dedup_count():
    store, wm, _ = _make_store()
    # simulate MemUpdateChecker dropping one as duplicate: only 1 written back
    wm.add_memories = AsyncMock(return_value=[MagicMock()])
    n = await store.promote([_item("semantic_memory", "a", "x"), _item("semantic_memory", "b", "y")])
    assert n == 1


# ---------------------------------------------------------------- lock mutex


@pytest.mark.asyncio
async def test_concurrent_promotes_do_not_overlap():
    """
    Two concurrent promotes on the same user serialize: their critical
    sections (write_manager.add_memories) never run at the same time.
    """
    store, wm, _ = _make_store()
    active = 0
    max_active = 0

    async def tracking_add(u, s, mems, llm):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)          # hold the critical section long enough to expose overlap
        active -= 1
        return [m for lst in mems.values() for m in lst]

    wm.add_memories = AsyncMock(side_effect=tracking_add)

    await asyncio.gather(
        store.promote([_item("semantic_memory", "a", "x")]),
        store.promote([_item("semantic_memory", "b", "y")]),
    )
    assert max_active == 1                  # the user lock prevented overlap


@pytest.mark.asyncio
async def test_prepare_write_runs_inside_lock_before_add():
    """
    prepare_write (e.g. apply scope embedding) runs before add_memories, and
    while the user lock is held.
    """
    store, wm, kv = _make_store()
    order = []

    async def prepare():
        order.append("prepare")
        # lock must already be held when prepare runs
        assert "_lock/user/u" in [c.args[0] for c in kv.exclusive_set.call_args_list]

    async def add(u, s, mems, llm):
        order.append("add")
        return [m for lst in mems.values() for m in lst]

    store._prepare_write = prepare
    wm.add_memories = AsyncMock(side_effect=add)

    await store.promote([_item("semantic_memory", "t", "c")])
    assert order == ["prepare", "add"]


@pytest.mark.asyncio
async def test_prepare_write_skipped_when_nothing_to_write():
    """No valid items → no lock, no prepare_write, no add."""
    store, wm, kv = _make_store()
    prepare = AsyncMock()
    store._prepare_write = prepare
    assert await store.promote([_item("garbage")]) == 0
    prepare.assert_not_called()
    wm.add_memories.assert_not_called()


@pytest.mark.asyncio
async def test_promote_waits_for_externally_held_user_lock():
    """
    promote blocks while the SAME user lock add_messages uses is held
    elsewhere, and proceeds once released.
    """
    store, wm, kv = _make_store()

    external = DistributedLock(kv, "user/u")   # same key add_messages takes (long_term_memory.py)
    await external.acquire()

    done = asyncio.Event()

    async def run_promote():
        await store.promote([_item("semantic_memory", "a", "x")])
        done.set()

    task = asyncio.create_task(run_promote())
    await asyncio.sleep(0.05)
    assert not done.is_set()                # blocked: external holder owns the lock
    wm.add_memories.assert_not_called()     # never entered the critical section

    await external.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert done.is_set()
    wm.add_memories.assert_awaited_once()
