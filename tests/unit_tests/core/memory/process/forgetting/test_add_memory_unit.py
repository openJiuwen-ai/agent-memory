# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# pylint: disable=protected-access
"""
Step 7 unit tests — ``LongTermMemory.add_memory_unit`` and the
``filters`` FilterGroup on ``SearchManager.list_user_mem`` / ``SearchParams``.

Scope:
  - ``add_memory_unit`` validation:
      - empty scope_id / user_id → MEMORY_ADD_MEMORY_UNIT_ERROR
      - empty memory_doc.id → MEMORY_ADD_MEMORY_UNIT_ERROR
      - empty memory_doc.text → MEMORY_ADD_MEMORY_UNIT_ERROR
  - ``add_memory_unit`` happy path:
      - writes through memory_index.add_memories
      - forces blacklisted=False (recall = un-forget)
      - returns memory_doc.id
  - ``add_memory_unit`` retrieve_history seed:
      - when retrieve_history dict is passed, KV is seeded at the
        retrieve_history key with JSON of that dict
  - ``add_memory_unit`` write failure → MEMORY_ADD_MEMORY_UNIT_ERROR
  - ``SearchParams`` accepts ``filters`` field
  - ``SearchManager.search`` auto-injects NE("blacklisted", True) when
    filters=None
  - ``SearchManager.search`` keeps caller filters as-is when they
    already mention the ``blacklisted`` field
  - ``SearchManager.list_user_mem`` default excludes blacklisted; with
    a caller filter that mentions ``blacklisted`` (e.g.
    EQ("blacklisted", True)) the caller's filters are kept as-is
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.filter_dsl import FilterCondition, FilterGroup, FilterOperator
from jiuwen_memory.memory_core.manage.index.fragment_memory_manager import FragmentMemoryManager
from jiuwen_memory.memory_core.manage.index.summary_manager import SummaryManager
from jiuwen_memory.memory_core.manage.search.search_manager import SearchManager, SearchParams
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory


def _dict_kv_backed():
    """Async-mockable KV store backed by a real dict."""
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

    kv = MagicMock()
    kv.exclusive_set = AsyncMock(side_effect=_exclusive_set)
    kv.get = AsyncMock(side_effect=_get)
    kv.set = AsyncMock(side_effect=_set)
    kv.delete = AsyncMock(side_effect=_delete)
    return kv, backing


@pytest.fixture
def ltm():
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()
    kv, _ = _dict_kv_backed()
    m.kv_store = kv
    m.memory_index = MagicMock()
    m.memory_index.add_memories = AsyncMock()
    m.memory_index.update_mem_by_id = AsyncMock()
    m.memory_index.search = AsyncMock(return_value=[])
    m.memory_index.list_memories = AsyncMock(return_value=[])
    yield m
    Singleton._instances.pop(LongTermMemory, None)


def _mem_doc(mem_id="m1", text="hello", blacklisted=True):
    """A MemoryDoc that simulates a previously-forgotten memory."""
    return MemoryDoc(
        id=mem_id,
        text=text,
        type="semantic_memory",
        timestamp=datetime.now(timezone.utc).astimezone(),
        fields={},
        blacklisted=blacklisted,
    )


# ---------------------------------------------------------------- validation


@pytest.mark.asyncio
async def test_add_memory_unit_empty_scope_id_raises(ltm):
    with pytest.raises(BaseError):
        await ltm.add_memory_unit(
            scope_id="", user_id="u", memory_doc=_mem_doc(),
        )
    ltm.memory_index.add_memories.assert_not_called()


@pytest.mark.asyncio
async def test_add_memory_unit_empty_user_id_raises(ltm):
    with pytest.raises(BaseError):
        await ltm.add_memory_unit(
            scope_id="s", user_id="", memory_doc=_mem_doc(),
        )


@pytest.mark.asyncio
async def test_add_memory_unit_empty_mem_id_raises(ltm):
    with pytest.raises(BaseError):
        await ltm.add_memory_unit(
            scope_id="s", user_id="u", memory_doc=_mem_doc(mem_id=""),
        )


@pytest.mark.asyncio
async def test_add_memory_unit_empty_text_raises(ltm):
    with pytest.raises(BaseError):
        await ltm.add_memory_unit(
            scope_id="s", user_id="u", memory_doc=_mem_doc(text=""),
        )


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_add_memory_unit_writes_and_returns_id(ltm):
    doc = _mem_doc(mem_id="m1", text="hello", blacklisted=True)
    ret = await ltm.add_memory_unit(
        scope_id="s", user_id="u", memory_doc=doc,
    )
    assert ret == "m1"
    ltm.memory_index.add_memories.assert_awaited_once()
    args, _ = ltm.memory_index.add_memories.call_args
    user_id, scope_id, docs = args
    assert (user_id, scope_id) == ("u", "s")
    assert len(docs) == 1
    assert docs[0].id == "m1"
    assert docs[0].text == "hello"


@pytest.mark.asyncio
async def test_add_memory_unit_forces_blacklisted_false(ltm):
    """
    Recall = un-forget. Even if the caller passed a doc with
    blacklisted=True, the written doc must have blacklisted=False so the
    memory is once again searchable.
    """
    doc = _mem_doc(mem_id="m1", blacklisted=True)
    await ltm.add_memory_unit(scope_id="s", user_id="u", memory_doc=doc)
    written = ltm.memory_index.add_memories.call_args.args[2][0]
    assert written.blacklisted is False


# ---------------------------------------------------------------- retrieve_history seed


@pytest.mark.asyncio
async def test_add_memory_unit_seeds_retrieve_history_when_provided(ltm):
    """
    When retrieve_history is passed, KV is seeded at the retrieve_history
    key with the JSON of that dict.
    """
    rh_payload = {
        "retrieve_count": 5,
        "latest_retrieve_time": "2026-07-21T10:00:00+08:00",
        "retrieve_history": ["2026-07-01T10:00:00+08:00"],
    }
    doc = _mem_doc(mem_id="m1")
    await ltm.add_memory_unit(
        scope_id="s", user_id="u", memory_doc=doc,
        retrieve_history=rh_payload,
    )
    # KV should have a key matching the retrieve_history prefix
    from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
    sep = VariableManager.SEPARATOR
    expected_key = f"retrieve_history{sep}u{sep}s{sep}m1"
    written = await ltm.kv_store.get(expected_key)
    assert written is not None
    parsed = json.loads(written)
    assert parsed["retrieve_count"] == 5
    assert parsed["retrieve_history"] == ["2026-07-01T10:00:00+08:00"]


@pytest.mark.asyncio
async def test_add_memory_unit_skips_retrieve_history_when_none(ltm):
    """When retrieve_history is None, the KV key is NOT written."""
    doc = _mem_doc(mem_id="m1")
    await ltm.add_memory_unit(
        scope_id="s", user_id="u", memory_doc=doc,
        retrieve_history=None,
    )
    from jiuwen_memory.memory_core.manage.index.variable_manager import VariableManager
    sep = VariableManager.SEPARATOR
    expected_key = f"retrieve_history{sep}u{sep}s{sep}m1"
    written = await ltm.kv_store.get(expected_key)
    assert written is None


# ---------------------------------------------------------------- blacklist cleanup


# ---------------------------------------------------------------- write failure


@pytest.mark.asyncio
async def test_add_memory_unit_write_failure_raises_unit_error(ltm):
    ltm.memory_index.add_memories = AsyncMock(side_effect=RuntimeError("storage down"))
    doc = _mem_doc(mem_id="m1")
    with pytest.raises(BaseError):
        await ltm.add_memory_unit(scope_id="s", user_id="u", memory_doc=doc)


# ---------------------------------------------------------------- SearchParams


def test_search_params_default_filters_none():
    params = SearchParams(
        user_id="u", scope_id="s", query="hello",
    )
    assert params.filters is None


def test_search_params_accepts_explicit_filters():
    custom = FilterGroup(conditions=[
        FilterCondition(field="is_important", op=FilterOperator.EQ, value=True)
    ])
    params = SearchParams(
        user_id="u", scope_id="s", query="hello", filters=custom,
    )
    assert params.filters is custom


def test_search_params_rejects_dict_filters():
    """Callers must construct FilterGroup explicitly — dict is NOT accepted.
    The rejection happens at normalize_filters time when
    search() is invoked, not at SearchParams construction time (pydantic
    arbitrary_types_allowed lets the dict through at construction).
    """
    from jiuwen_memory.memory_core.manage.search.filter_normalizer import normalize_filters
    with pytest.raises(BaseError):
        normalize_filters({"blacklisted": False})  # dict — must be rejected


# ---------------------------------------------------------------- SearchManager.search filter injection


def _managers_for_search():
    """Build a SearchManager with fragment + summary managers backed by a
    dict-style memory_index."""
    kv, _ = _dict_kv_backed()
    memory_index = MagicMock()
    memory_index.search = AsyncMock(return_value=[])
    memory_index.list_memories = AsyncMock(return_value=[])
    memory_index.add_memories = AsyncMock()
    memory_index.update_mem_by_id = AsyncMock()

    frag = FragmentMemoryManager(memory_index=memory_index, crypto_key=b"k")
    summ = SummaryManager(memory_index=memory_index, crypto_key=b"k")
    managers = {
        MemoryType if False else "user_profile": frag,
        "semantic_memory": frag,
        "episodic_memory": frag,
        "summary": summ,
    }
    # Use str keys for the manager dict (SearchManager iterates by str type)
    from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType as Mt
    managers = {
        Mt.USER_PROFILE.value: frag,
        Mt.SEMANTIC_MEMORY.value: frag,
        Mt.EPISODIC_MEMORY.value: frag,
        Mt.SUMMARY.value: summ,
    }
    search_mgr = SearchManager(
        managers=managers, crypto_key=b"k", memory_index=memory_index,
    )
    return search_mgr, memory_index


@pytest.mark.asyncio
async def test_search_auto_injects_blacklisted_ne_by_default():
    """filters=None (default) → framework injects
    NE("blacklisted", True) and forwards it to manager.search → memory_index.search.
    """
    search_mgr, memory_index = _managers_for_search()
    params = SearchParams(
        user_id="u", scope_id="s", query="hello", top_k=5,
        search_type=["semantic_memory"],
    )
    await search_mgr.search(params)
    memory_index.search.assert_awaited_once()
    forwarded = memory_index.search.call_args.kwargs.get("filters")
    assert forwarded is not None
    # The injected group must contain a NE("blacklisted", True) condition
    found = False
    for cond in forwarded.conditions:
        if getattr(cond, "field", None) == "blacklisted" and \
                getattr(cond, "op", None) == FilterOperator.NE and \
                getattr(cond, "value", None) is True:
            found = True
            break
    assert found, "expected NE('blacklisted', True) in forwarded filters"


@pytest.mark.asyncio
async def test_search_filters_eq_blacklisted_true_kept_as_is():
    """
    Caller-supplied EQ("blacklisted", True) → framework does NOT inject
    an extra NE; the caller's filters are forwarded as-is so blacklisted
    memories are surfaced.
    """
    search_mgr, memory_index = _managers_for_search()
    custom = FilterGroup(conditions=[
        FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True)
    ])
    params = SearchParams(
        user_id="u", scope_id="s", query="hello", top_k=5,
        search_type=["semantic_memory"],
        filters=custom,
    )
    await search_mgr.search(params)
    forwarded = memory_index.search.call_args.kwargs.get("filters")
    # The caller's group is returned as-is (no extra NE injected)
    assert forwarded is custom or forwarded == custom
    # Exactly one condition targeting "blacklisted"
    bl_conds = [c for c in forwarded.conditions
                if getattr(c, "field", None) == "blacklisted"]
    assert len(bl_conds) == 1
    assert bl_conds[0].op == FilterOperator.EQ
    assert bl_conds[0].value is True


@pytest.mark.asyncio
async def test_search_caller_filters_with_blacklisted_kept_as_is():
    """
    If the caller's FilterGroup already mentions the blacklisted field,
    the framework must NOT inject another NE.
    """
    search_mgr, memory_index = _managers_for_search()
    custom = FilterGroup(conditions=[
        FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True)
    ])
    params = SearchParams(
        user_id="u", scope_id="s", query="hello", top_k=5,
        search_type=["semantic_memory"],
        filters=custom,
    )
    await search_mgr.search(params)
    forwarded = memory_index.search.call_args.kwargs.get("filters")
    # The caller's group is returned as-is (no extra NE injected)
    assert forwarded is custom or forwarded == custom
    # Exactly one condition targeting "blacklisted"
    bl_conds = [c for c in forwarded.conditions
                if getattr(c, "field", None) == "blacklisted"]
    assert len(bl_conds) == 1


@pytest.mark.asyncio
async def test_list_user_mem_default_excludes_blacklisted():
    """
    list_user_mem with filters=None (default) forwards a non-None filters
    group to memory_index.list_memories — the backend is expected to honour
    it.
    """
    search_mgr, memory_index = _managers_for_search()
    await search_mgr.list_user_mem(
        user_id="u", scope_id="s", nums=10, pages=1,
    )
    memory_index.list_memories.assert_awaited_once()
    forwarded = memory_index.list_memories.call_args.kwargs.get("filters")
    assert forwarded is not None
    # The group must mention "blacklisted" (the default NE injection).
    bl_conds = [c for c in forwarded.conditions
                if getattr(c, "field", None) == "blacklisted"]
    assert len(bl_conds) == 1


@pytest.mark.asyncio
async def test_list_user_mem_filters_eq_blacklisted_true_kept():
    """
    list_user_mem with a caller filter EQ("blacklisted", True) → framework
    does NOT inject an extra NE; the caller's filters are forwarded as-is so
    blacklisted memories are surfaced (recall path).
    """
    search_mgr, memory_index = _managers_for_search()
    custom = FilterGroup(conditions=[
        FilterCondition(field="blacklisted", op=FilterOperator.EQ, value=True)
    ])
    await search_mgr.list_user_mem(
        user_id="u", scope_id="s", nums=10, pages=1, filters=custom,
    )
    forwarded = memory_index.list_memories.call_args.kwargs.get("filters")
    # The caller's group is forwarded as-is — exactly one blacklisted cond.
    bl_conds = [c for c in forwarded.conditions
                if getattr(c, "field", None) == "blacklisted"]
    assert len(bl_conds) == 1
    assert bl_conds[0].op == FilterOperator.EQ
    assert bl_conds[0].value is True
