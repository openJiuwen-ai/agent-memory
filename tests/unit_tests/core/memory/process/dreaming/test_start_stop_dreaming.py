# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Tests reset the Singleton and drive LongTermMemory/orchestrator internals.
# pylint: disable=protected-access

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.common.exception.errors import BaseError
from jiuwen_memory.common.utils.singleton import Singleton
from jiuwen_memory.memory_core.config import DreamingConfig
from jiuwen_memory.memory_core.long_term_memory import LongTermMemory


@pytest.fixture
def ltm():
    Singleton._instances.pop(LongTermMemory, None)
    m = LongTermMemory()
    m.message_manager = MagicMock()
    m.write_manager = MagicMock()
    m.kv_store = MagicMock()
    m._get_scope_llm = AsyncMock(return_value="LLM")
    yield m
    Singleton._instances.pop(LongTermMemory, None)


@pytest.mark.asyncio
async def test_start_creates_registers_and_runs(ltm):
    orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    try:
        assert orch is not None
        assert orch.health["running"] is True
        assert ltm._dreaming_orchestrators[("s", "u")] is orch
    finally:
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_idempotent_same_scope_user(ltm):
    o1 = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    o2 = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    try:
        assert o1 is o2
        assert len(ltm._dreaming_orchestrators) == 1
    finally:
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_disabled_config_returns_none(ltm):
    orch = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=False))
    assert orch is None
    assert ltm._dreaming_orchestrators == {}


@pytest.mark.asyncio
async def test_default_config_is_disabled(ltm):
    # no config → DreamingConfig() → enabled defaults False → no-op
    orch = await ltm.start_dreaming("s", "u")
    assert orch is None
    assert ltm._dreaming_orchestrators == {}


@pytest.mark.asyncio
async def test_invalid_scope_id_raises(ltm):
    # empty scope_id fails _validate_id, consistent with other public APIs
    with pytest.raises(BaseError):
        await ltm.start_dreaming("", "u", config=DreamingConfig(enabled=True))
    assert ltm._dreaming_orchestrators == {}


@pytest.mark.asyncio
async def test_missing_stores_raises(ltm):
    ltm.write_manager = None
    with pytest.raises(BaseError):
        await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))


@pytest.mark.asyncio
async def test_missing_llm_raises(ltm):
    ltm._get_scope_llm = AsyncMock(return_value=None)
    with pytest.raises(BaseError):
        await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    assert ltm._dreaming_orchestrators == {}      # not registered on failure


@pytest.mark.asyncio
async def test_orchestrator_name_carries_scope_user(ltm):
    orch = await ltm.start_dreaming("scopeX", "userY", config=DreamingConfig(enabled=True))
    try:
        assert orch._name == "dreaming/scopeX/userY"
    finally:
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_stop_specific_leaves_others(ltm):
    await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    await ltm.start_dreaming("s2", "u2", config=DreamingConfig(enabled=True))
    await ltm.stop_dreaming("s", "u")
    try:
        assert ("s", "u") not in ltm._dreaming_orchestrators
        assert ("s2", "u2") in ltm._dreaming_orchestrators
        assert ltm._dreaming_orchestrators[("s2", "u2")].health["running"] is True
    finally:
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_stop_all_with_no_args(ltm):
    o1 = await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    o2 = await ltm.start_dreaming("s2", "u2", config=DreamingConfig(enabled=True))
    await ltm.stop_dreaming()
    assert ltm._dreaming_orchestrators == {}
    assert o1.health["running"] is False
    assert o2.health["running"] is False


@pytest.mark.asyncio
async def test_stop_filters_by_user_only(ltm):
    await ltm.start_dreaming("s", "u", config=DreamingConfig(enabled=True))
    await ltm.start_dreaming("s2", "u", config=DreamingConfig(enabled=True))
    await ltm.start_dreaming("s", "other", config=DreamingConfig(enabled=True))
    await ltm.stop_dreaming(user_id="u")          # stop both u entries, keep "other"
    try:
        assert set(ltm._dreaming_orchestrators.keys()) == {("s", "other")}
    finally:
        await ltm.stop_dreaming()


@pytest.mark.asyncio
async def test_stop_unknown_is_noop(ltm):
    await ltm.stop_dreaming("nope", "nobody")     # nothing registered → no error
    assert ltm._dreaming_orchestrators == {}
