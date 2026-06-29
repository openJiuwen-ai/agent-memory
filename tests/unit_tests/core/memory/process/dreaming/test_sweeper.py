# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwen_memory.memory_core.config.config import DreamingConfig
from jiuwen_memory.memory_core.process.dreaming.source import NormalizedSession
from jiuwen_memory.memory_core.process.dreaming.sweeper import Sweeper, _compress, _events_tokens


# ---------------------------------------------------------------- _compress

def test_compress_converges_within_budget():
    events = [{"role": "user", "content": "x" * 100} for _ in range(50)]
    out = _compress(events, max_tokens=50)
    assert _events_tokens(out) <= 50 or len(out) == 2     # converged
    # keeps oldest and newest
    assert out[0] is events[0]
    assert out[-1] is events[-1]


def test_compress_noop_when_within_budget():
    events = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert _compress(events, max_tokens=10_000) == events


# ---------------------------------------------------------------- prompt

def test_extraction_prompt_md_renders():
    from jiuwen_memory.memory_core.prompts.prompt_applier import PromptApplier
    p = PromptApplier().apply("dreaming_extraction", {"dialogue": "user: hello", "max_items": "3"})
    assert "user: hello" in p
    assert "最多输出 3 条" in p
    assert "{{dialogue}}" not in p and "{{max_items}}" not in p


# ---------------------------------------------------------------- helpers

def _llm_returning(payload):
    """LLM whose invoke returns a response with .content = json payload."""
    resp = MagicMock()
    resp.content = payload if isinstance(payload, str) else json.dumps(payload)
    llm = MagicMock()
    llm.invoke = AsyncMock(return_value=resp)
    return llm


def _kv():
    backing = {}
    kv = MagicMock()
    kv.get = AsyncMock(side_effect=lambda k: backing.get(k))
    kv.set = AsyncMock(side_effect=lambda k, v: backing.__setitem__(k, v))
    return kv, backing


def _sweeper(source, store, llm, kv, config=None):
    return Sweeper(
        source=source, store=store, llm=llm,
        config=config or DreamingConfig(max_items_per_session=5),
        checkpoint_io=kv, user_id="u", scope_id="s",
    )


def _source_returning(sessions):
    src = MagicMock()
    src.iter_new_sessions = AsyncMock(return_value=sessions)
    return src


# ---------------------------------------------------------------- run_sweep

@pytest.mark.asyncio
async def test_run_sweep_end_to_end():
    sessions = [NormalizedSession("sess_a", [{"role": "user", "content": "I am a data analyst"}])]
    src = _source_returning(sessions)
    store = MagicMock()
    store.promote = AsyncMock(return_value=1)
    llm = _llm_returning([{"mem_type": "user_profile", "title": "job", "content": "data analyst"}])
    kv, backing = _kv()

    await _sweeper(src, store, llm, kv).run_sweep()

    # promoted the extracted item, tagged with the source session
    store.promote.assert_awaited_once()
    items = store.promote.call_args.args[0]
    assert len(items) == 1
    assert items[0].mem_type == "user_profile"
    assert items[0].source_session_id == "sess_a"
    # checkpoint advanced
    saved = json.loads(backing["dreaming/checkpoint/s/u"])
    assert saved["scanned_sessions"] == ["sess_a"]


@pytest.mark.asyncio
async def test_no_new_sessions_skips_everything():
    src = _source_returning([])
    store = MagicMock()
    store.promote = AsyncMock()
    kv, backing = _kv()

    await _sweeper(src, store, _llm_returning([]), kv).run_sweep()

    store.promote.assert_not_called()
    assert backing == {}            # no checkpoint write


@pytest.mark.asyncio
async def test_loads_existing_checkpoint_and_passes_to_source():
    kv, backing = _kv()
    backing["dreaming/checkpoint/s/u"] = json.dumps({"scanned_sessions": ["old1", "old2"]})
    src = _source_returning([])
    store = MagicMock()
    store.promote = AsyncMock()

    await _sweeper(src, store, _llm_returning([]), kv).run_sweep()

    scanned_arg = src.iter_new_sessions.call_args.args[0]
    assert scanned_arg == {"old1", "old2"}


@pytest.mark.asyncio
async def test_failed_session_not_checkpointed_others_proceed():
    sessions = [
        NormalizedSession("good", [{"role": "user", "content": "a"}]),
        NormalizedSession("bad", [{"role": "user", "content": "b"}]),
    ]
    src = _source_returning(sessions)
    store = MagicMock()
    store.promote = AsyncMock(return_value=1)
    kv, backing = _kv()

    # llm returns valid JSON for "good", junk (unparseable) for "bad"
    resp_good = MagicMock()
    resp_good.content = json.dumps([{"mem_type": "semantic_memory", "title": "t", "content": "c"}])
    resp_bad = MagicMock()
    resp_bad.content = "not json at all"
    llm = MagicMock()
    llm.invoke = AsyncMock(side_effect=lambda messages: resp_bad if "b" in messages[0]["content"] else resp_good)

    await _sweeper(src, store, llm, kv).run_sweep()

    saved = json.loads(backing["dreaming/checkpoint/s/u"])
    assert saved["scanned_sessions"] == ["good"]        # "bad" not marked
    items = store.promote.call_args.args[0]
    assert [i.source_session_id for i in items] == ["good"]


@pytest.mark.asyncio
async def test_promote_failure_does_not_advance_checkpoint():
    sessions = [NormalizedSession("sess_a", [{"role": "user", "content": "a"}])]
    src = _source_returning(sessions)
    store = MagicMock()
    store.promote = AsyncMock(side_effect=RuntimeError("vector store down"))
    llm = _llm_returning([{"mem_type": "semantic_memory", "title": "t", "content": "c"}])
    kv, backing = _kv()

    with pytest.raises(RuntimeError):
        await _sweeper(src, store, llm, kv).run_sweep()

    assert "dreaming/checkpoint/s/u" not in backing      # checkpoint NOT advanced


@pytest.mark.asyncio
async def test_empty_extraction_still_checkpoints():
    sessions = [NormalizedSession("sess_a", [{"role": "user", "content": "small talk"}])]
    src = _source_returning(sessions)
    store = MagicMock()
    store.promote = AsyncMock(return_value=0)
    llm = _llm_returning([])                              # nothing worth remembering
    kv, backing = _kv()

    await _sweeper(src, store, llm, kv).run_sweep()

    store.promote.assert_not_called()                    # no items → no write
    saved = json.loads(backing["dreaming/checkpoint/s/u"])
    assert saved["scanned_sessions"] == ["sess_a"]      # but session marked done


@pytest.mark.asyncio
async def test_caps_items_per_session():
    many = [{"mem_type": "semantic_memory", "title": f"t{i}", "content": f"c{i}"} for i in range(10)]
    sessions = [NormalizedSession("sess_a", [{"role": "user", "content": "x"}])]
    src = _source_returning(sessions)
    store = MagicMock()
    store.promote = AsyncMock(return_value=2)
    kv, _ = _kv()

    await _sweeper(src, store, _llm_returning(many), kv,
                   config=DreamingConfig(max_items_per_session=2)).run_sweep()

    items = store.promote.call_args.args[0]
    assert len(items) == 2
