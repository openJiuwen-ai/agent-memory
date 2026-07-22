# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# pylint: disable=protected-access
"""
Step 6 unit tests — ``_mark_blacklisted`` and ``_forget_memories_step``.

Scope:
  - ``_mark_blacklisted`` empty list → no-op
  - ``_mark_blacklisted`` vector scalar flip via ``update_mem_by_id``
    (called with ``fields={"blacklisted": True}``, NOT keyword args)
  - ``_mark_blacklisted`` vector failure isolated — other docs still flipped
  - ``_forget_memories_step`` paginated sweep — multiple list_memories pages
  - ``_forget_memories_step`` mem_type whitelist excludes variable / middle_term
  - ``_forget_memories_step`` user-level lock acquired (``DistributedLock(kv, "user/{u}")``)
  - ``_forget_memories_step`` evaluator result is blacklisted (vector scalar)
  - ``_forget_memories_step`` evaluator exception is swallowed → warning
  - ``_forget_memories_step`` list_memories exception is swallowed → warning
  - ``_forget_memories_step`` evaluator=None falls back to EbbinghausEvaluator
  - start_dreaming sweep_fn closure: forgetting disabled → not called
  - start_dreaming sweep_fn closure: forgetting enabled → called after sweeper
  - start_dreaming sweep_fn closure: forget failure does NOT abort sweeper result
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.memory_core.config import DreamingConfig
from jiuwen_memory.memory_core.config.config import ForgettingConfig, MemoryScopeConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
from jiuwen_memory.memory_core.process.forgetting import ForgetContext, ForgetEvaluator


# ---------------------------------------------------------------- stub evaluator


class _StubEvaluator(ForgetEvaluator):
    """A non-abstract ForgetEvaluator whose evaluate is patched per-test."""

    async def evaluate(self, ctx: ForgetContext, memories):
        raise NotImplementedError("patch me in the test")


# ---------------------------------------------------------------- helpers


def _doc(mem_id: str, mem_type: str = "semantic_memory", text: str = "x") -> MemoryDoc:
    return MemoryDoc(
        id=mem_id,
        text=text,
        type=mem_type,
        timestamp=datetime.now(timezone.utc).astimezone(),
        fields={},
    )


def _dict_kv_backed():
    """Return (kv_store, backing_dict) where kv behaves like a real async KV."""
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
    # Stub out scope-llm fetches — only relevant for start_dreaming tests.
    m._get_scope_llm = AsyncMock(return_value="LLM")
    m.message_manager = MagicMock()
    m.write_manager = MagicMock()
    yield m
    Singleton._instances.pop(LongTermMemory, None)


# ---------------------------------------------------------------- _mark_blacklisted


@pytest.mark.asyncio
async def test_mark_blacklisted_empty_is_noop(ltm):
    ltm.memory_index.update_mem_by_id = AsyncMock()
    await ltm._mark_blacklisted(scope_id="s", user_id="u", evicted=[])
    ltm.memory_index.update_mem_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_mark_blacklisted_vector_uses_dict_fields_not_kwargs(ltm):
    """update_mem_by_id takes a dict, not keyword args.
    The real signature is ``update_mem_by_id(user_id, scope_id, mem_id, fields: dict)``.
    """
    ltm.memory_index.update_mem_by_id = AsyncMock()
    evicted = [_doc("m1"), _doc("m2")]
    await ltm._mark_blacklisted(scope_id="s", user_id="u", evicted=evicted)
    assert ltm.memory_index.update_mem_by_id.await_count == 2
    for call in ltm.memory_index.update_mem_by_id.call_args_list:
        args, kwargs = call
        # 4th positional arg is the fields dict — not a keyword arg
        assert len(args) == 4
        assert args[3] == {"blacklisted": True}
        assert "blacklisted" not in kwargs


@pytest.mark.asyncio
async def test_mark_blacklisted_vector_failure_isolated(ltm):
    """
    One vector update fails — others continue. The failed id is simply
    skipped; the next sweep will retry (the flip is idempotent). No KV
    shadow index to keep consistent — vector scalar is the only truth.
    """
    ltm.memory_index.update_mem_by_id = AsyncMock(
        side_effect=[RuntimeError("boom"), None]
    )
    evicted = [_doc("m1"), _doc("m2")]
    await ltm._mark_blacklisted(scope_id="s", user_id="u", evicted=evicted)
    # Both calls attempted — m1 failed, m2 succeeded, no abort.
    assert ltm.memory_index.update_mem_by_id.await_count == 2


# ---------------------------------------------------------------- _forget_memories_step


@pytest.mark.asyncio
async def test_forget_step_paginates_and_calls_evaluator(ltm):
    """
    list_memories is paginated in batches of 100; the evaluator receives
    the flattened candidate list and returns evicted subset.
    """
    page1 = [_doc(f"m{i}") for i in range(100)]
    page2 = [_doc(f"m{i}") for i in range(100, 150)]   # short page → stop
    ltm.memory_index.list_memories = AsyncMock(side_effect=[page1, page2])
    ltm.memory_index.update_mem_by_id = AsyncMock()

    evaluator = _StubEvaluator()
    evicted = [_doc("m5"), _doc("m42")]
    evaluator.evaluate = AsyncMock(return_value=evicted)

    cfg = ForgettingConfig(enabled=True, evaluator=evaluator)
    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)

    # Two pages fetched
    assert ltm.memory_index.list_memories.await_count == 2
    # Evaluator received all 150 candidates
    candidates = evaluator.evaluate.call_args.args[1]
    assert len(candidates) == 150
    # Both evicted ids got vector-blacklisted
    blacklisted_ids = {
        call.args[2] for call in ltm.memory_index.update_mem_by_id.call_args_list
    }
    assert blacklisted_ids == {"m5", "m42"}


@pytest.mark.asyncio
async def test_forget_step_mem_type_whitelist_excludes_variable_and_middle(ltm):
    """
    forget only scans the four fragment+summary types. mem_types passed
    to list_memories must NOT include variable / middle_term_memory.
    """
    ltm.memory_index.list_memories = AsyncMock(return_value=[])
    ltm.memory_index.update_mem_by_id = AsyncMock()
    evaluator = _StubEvaluator()
    evaluator.evaluate = AsyncMock(return_value=[])
    cfg = ForgettingConfig(enabled=True, evaluator=evaluator)

    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)

    list_call = ltm.memory_index.list_memories.call_args
    # Implementation calls list_memories(user_id, scope_id, offset=...,
    # limit=..., mem_types=...) — mem_types is a keyword arg.
    mem_types_arg = list_call.kwargs["mem_types"]
    assert set(mem_types_arg) == {
        "user_profile", "semantic_memory", "episodic_memory", "summary"
    }
    assert "variable" not in mem_types_arg
    assert "middle_term_memory" not in mem_types_arg


@pytest.mark.asyncio
async def test_forget_step_acquires_user_level_lock(ltm):
    """
    forget must hold ``DistributedLock(kv_store, f"user/{user_id}")``
    for the duration of the sweep (not the same lock sweeper.run_sweep releases).
    """
    ltm.memory_index.list_memories = AsyncMock(return_value=[])
    ltm.memory_index.update_mem_by_id = AsyncMock()
    evaluator = _StubEvaluator()
    evaluator.evaluate = AsyncMock(return_value=[])
    cfg = ForgettingConfig(enabled=True, evaluator=evaluator)

    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)

    # _lock/user/u must be one of the locks acquired
    lock_keys = [c.args[0] for c in ltm.kv_store.exclusive_set.call_args_list]
    assert "_lock/user/u" in lock_keys


@pytest.mark.asyncio
async def test_forget_step_evaluator_exception_is_swallowed(ltm):
    """
    Evaluator raises → main flow logs a warning, does NOT re-raise.
    Next dreaming interval is the recovery path.
    """
    ltm.memory_index.list_memories = AsyncMock(return_value=[_doc("m1")])
    ltm.memory_index.update_mem_by_id = AsyncMock()
    evaluator = _StubEvaluator()
    evaluator.evaluate = AsyncMock(side_effect=RuntimeError("evaluator crashed"))
    cfg = ForgettingConfig(enabled=True, evaluator=evaluator)

    # Must not raise
    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)
    # No blacklist writes happened because evaluator raised before _mark_blacklisted
    ltm.memory_index.update_mem_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_forget_step_list_memories_exception_is_swallowed(ltm):
    """
    list_memories raises → main flow logs a warning, does NOT re-raise.
    """
    ltm.memory_index.list_memories = AsyncMock(side_effect=RuntimeError("storage down"))
    ltm.memory_index.update_mem_by_id = AsyncMock()
    evaluator = _StubEvaluator()
    evaluator.evaluate = AsyncMock(return_value=[])
    cfg = ForgettingConfig(enabled=True, evaluator=evaluator)

    # Must not raise
    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)
    ltm.memory_index.update_mem_by_id.assert_not_called()
    evaluator.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_forget_step_evaluator_none_falls_back_to_ebbinghaus(ltm):
    """
    ForgettingConfig(evaluator=None) → use built-in EbbinghausEvaluator
    constructed with this instance's kv_store.
    """
    ltm.memory_index.list_memories = AsyncMock(return_value=[])
    ltm.memory_index.update_mem_by_id = AsyncMock()
    cfg = ForgettingConfig(enabled=True, evaluator=None)

    await ltm._forget_memories_step(scope_id="s", user_id="u", forgetting_config=cfg)
    # The fallback evaluator still ran (it received an empty candidate list
    # and produced no evictions). If the fallback path had raised, the
    # whole call would have surfaced the error.
    ltm.memory_index.list_memories.assert_awaited_once()


# ---------------------------------------------------------------- start_dreaming closure


@pytest.mark.asyncio
async def test_sweep_fn_forgetting_disabled_not_called(ltm):
    """
    DreamingConfig.forgetting is None or .enabled=False → sweep_fn must
    NOT call _forget_memories_step.
    """
    captured = {}

    class _StubSweeper:
        def __init__(self, **kwargs):
            pass

        async def run_sweep(self):
            captured["sweep_ran"] = True

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    forget_called = []
    ltm._forget_memories_step = AsyncMock(
        side_effect=lambda **kw: forget_called.append(kw)
    )
    try:
        orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
        assert orch is not None
        # Drive one sweep manually (orchestrator interval is 4h by default)
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert captured.get("sweep_ran") is True
        assert forget_called == []   # forgetting not configured
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_sweep_fn_forgetting_enabled_called_after_sweeper(ltm):
    """
    DreamingConfig.forgetting.enabled=True → _forget_memories_step runs
    AFTER sweeper.run_sweep completes.
    """
    sweep_order = []

    class _StubSweeper:
        def __init__(self, **kwargs):
            pass

        async def run_sweep(self):
            sweep_order.append("sweep")

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    async def fake_forget(**kw):
        sweep_order.append("forget")

    ltm._forget_memories_step = AsyncMock(side_effect=fake_forget)
    try:
        forgetting = ForgettingConfig(enabled=True)
        orch = await ltm.start_dreaming(
            "s", "u", config=DreamingConfig(enabled=True, forgetting=forgetting),
        )
        assert orch is not None
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert sweep_order == ["sweep", "forget"]
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_sweep_fn_forgetting_failure_does_not_break_subsequent_cycles(ltm):
    """
    _forget_memories_step raises → sweep_fn must catch it (the next
    dreaming interval still runs sweeper.run_sweep).
    """
    call_count = {"sweep": 0, "forget": 0}

    class _StubSweeper:
        def __init__(self, **kwargs):
            pass

        async def run_sweep(self):
            call_count["sweep"] += 1

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    async def fake_forget(**kw):
        call_count["forget"] += 1
        raise RuntimeError("forget exploded")

    ltm._forget_memories_step = AsyncMock(side_effect=fake_forget)
    try:
        forgetting = ForgettingConfig(enabled=True)
        orch = await ltm.start_dreaming(
            "s", "u", config=DreamingConfig(enabled=True, forgetting=forgetting),
        )
        # Run two cycles — both should complete, neither should raise
        await orch._sweep_fn()  # type: ignore[attr-defined]
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert call_count["sweep"] == 2
        assert call_count["forget"] == 2
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_start_dreaming_uses_scope_config_for_important_definition(ltm):
    """
    start_dreaming should try to read scope config for
    important_memory_definition; failures fall back to the global default.
    """
    # Force _get_scope_config to raise; the warning must be logged and
    # start_dreaming must still succeed (returns an orchestrator).
    ltm._get_scope_config = AsyncMock(side_effect=RuntimeError("scope not found"))

    class _StubSweeper:
        def __init__(self, **kw):
            self.kwargs = kw

        async def run_sweep(self):
            pass

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper
    try:
        orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
        assert orch is not None
        # No assertion on sweeper internals — the mere fact that start_dreaming
        # didn't raise and returned an orchestrator confirms the fallback path.
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


# ---------------------------------------------------------------- decoupled switches
# DreamingConfig.enabled and ForgettingConfig.enabled
# are independent flags. The orchestrator runs as long as EITHER is True;
# within a tick, sweeper.run_sweep() runs iff DreamingConfig.enabled, and
# _forget_memories_step runs iff ForgettingConfig.enabled.


@pytest.mark.asyncio
async def test_start_dreaming_both_disabled_returns_none(ltm):
    """
    Both DreamingConfig.enabled=False and ForgettingConfig.enabled=False
    (the latter via forgetting=None) → start_dreaming returns None, no
    orchestrator registered, no Sweeper constructed.
    """
    sweep_constructed = []

    class _SpySweeper:
        def __init__(self, **kw):
            sweep_constructed.append(kw)

        async def run_sweep(self):
            pass

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _SpySweeper
    try:
        orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=False))
        assert orch is None
        assert sweep_constructed == []
        assert ("s", "u") not in ltm._dreaming_orchestrators
    finally:
        ltm_mod.Sweeper = original_sweeper


@pytest.mark.asyncio
async def test_start_dreaming_dreaming_off_forgetting_on_skips_sweeper(ltm):
    """
    DreamingConfig.enabled=False + ForgettingConfig.enabled=True →
    orchestrator starts; sweep_fn must SKIP sweeper.run_sweep and STILL
    call _forget_memories_step. This is the common case for tenants who
    want Ebbinghaus forgetting without offline consolidation.
    """
    sweep_calls = []
    forget_calls = []

    class _StubSweeper:
        def __init__(self, **kw):
            pass

        async def run_sweep(self):
            sweep_calls.append("sweep")

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    async def fake_forget(**kw):
        forget_calls.append("forget")

    ltm._forget_memories_step = AsyncMock(side_effect=fake_forget)
    try:
        forgetting = ForgettingConfig(enabled=True)
        orch = await ltm.start_dreaming(
            "s", "u",
            config=DreamingConfig(enabled=False, forgetting=forgetting),
        )
        assert orch is not None
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert sweep_calls == []           # sweeper NOT called
        assert forget_calls == ["forget"]  # forget still ran
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_start_dreaming_dreaming_on_forgetting_off_runs_sweeper_only(ltm):
    """
    DreamingConfig.enabled=True + ForgettingConfig.enabled=False (via
    forgetting=None) → orchestrator runs; sweep_fn runs sweeper, does NOT
    call _forget_memories_step.
    """
    sweep_calls = []
    forget_calls = []

    class _StubSweeper:
        def __init__(self, **kw):
            pass

        async def run_sweep(self):
            sweep_calls.append("sweep")

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    async def fake_forget(**kw):
        forget_calls.append("forget")

    ltm._forget_memories_step = AsyncMock(side_effect=fake_forget)
    try:
        orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
        assert orch is not None
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert sweep_calls == ["sweep"]
        assert forget_calls == []
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_start_dreaming_both_on_runs_both_in_order(ltm):
    """
    Both flags True → sweeper runs first, forget runs second (so newly
    promoted memories get their retrieve_history seeded before being scored).
    """
    order: list[str] = []

    class _StubSweeper:
        def __init__(self, **kw):
            pass

        async def run_sweep(self):
            order.append("sweep")

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    async def fake_forget(**kw):
        order.append("forget")

    ltm._forget_memories_step = AsyncMock(side_effect=fake_forget)
    try:
        forgetting = ForgettingConfig(enabled=True)
        orch = await ltm.start_dreaming(
            "s", "u",
            config=DreamingConfig(enabled=True, forgetting=forgetting),
        )
        assert orch is not None
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert order == ["sweep", "forget"]
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_start_dreaming_forgetting_enabled_default_dreaming_disabled(ltm):
    """
    Bare DreamingConfig() defaults to enabled=False; explicitly setting
    only forgetting.enabled=True must still produce an orchestrator. This
    covers the 'forgetting-only' tenant setup where the caller never touches
    DreamingConfig.enabled at all.
    """
    sweep_calls = []

    class _StubSweeper:
        def __init__(self, **kw):
            pass

        async def run_sweep(self):
            sweep_calls.append("sweep")

    import jiuwen_memory.memory_core.long_term_memory as ltm_mod
    original_sweeper = ltm_mod.Sweeper
    ltm_mod.Sweeper = _StubSweeper

    ltm._forget_memories_step = AsyncMock()
    try:
        # Default DreamingConfig + forgetting explicitly enabled
        cfg = DreamingConfig(forgetting=ForgettingConfig(enabled=True))
        assert cfg.enabled is False
        orch = await ltm.start_dreaming("s", "u", config=cfg)
        assert orch is not None
        await orch._sweep_fn()  # type: ignore[attr-defined]
        assert sweep_calls == []  # dreaming disabled → no sweeper
        ltm._forget_memories_step.assert_awaited_once()
    finally:
        ltm_mod.Sweeper = original_sweeper
        await ltm.stop_dreaming()
