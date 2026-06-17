# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundation.llm.schema.message import BaseMessage
from foundation.store.base_message_store import MessageMetadata
from memory_core.process.dreaming.source import MessageStoreSessionSource, NormalizedSession


def _row(role, content, session_id, ts=0):
    msg = BaseMessage(role=role, content=content)
    meta = MessageMetadata(
        message_id=f"{session_id}-{role}-{ts}",
        user_id="u",
        scope_id="s",
        session_id=session_id,
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        message_type=role,
    )
    return msg, meta


def _make_source(rows, *, min_rounds=2, max_sessions=10):
    store = MagicMock()
    store.get_messages = AsyncMock(return_value=rows)
    mm = MagicMock()
    mm.store = store
    return MessageStoreSessionSource(mm, "u", "s", min_rounds=min_rounds, max_sessions=max_sessions), store


def _rounds(rows):
    """Helper: build a session of `rows` user/assistant pairs."""
    out = []
    for i in range(rows):
        out.append(("user", f"q{i}"))
        out.append(("assistant", f"a{i}"))
    return out


@pytest.mark.asyncio
async def test_groups_by_session_and_filters_short():
    rows = []
    # sess_a: 2 rounds -> kept
    for r, (role, content) in enumerate([("user", "q0"), ("assistant", "a0"), ("user", "q1"), ("assistant", "a1")]):
        rows.append(_row(role, content, "sess_a", ts=r))
    # sess_b: 1 round -> dropped (min_rounds=2)
    rows.append(_row("user", "hi", "sess_b", ts=10))
    rows.append(_row("assistant", "yo", "sess_b", ts=11))

    source, store = _make_source(rows, min_rounds=2)
    sessions = await source.iter_new_sessions(set())

    assert [s.session_id for s in sessions] == ["sess_a"]
    assert sessions[0].events == [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    # full pull, no time window, asc order
    _, kwargs = store.get_messages.call_args
    assert kwargs["order_direction"] == "asc"
    assert kwargs["limit"] >= 1_000_000


@pytest.mark.asyncio
async def test_skips_scanned_sessions():
    rows = []
    for sid in ("sess_a", "sess_b"):
        for r, (role, content) in enumerate(_rounds(2)):
            rows.append(_row(role, content, sid, ts=r))

    source, _ = _make_source(rows, min_rounds=2)
    sessions = await source.iter_new_sessions({"sess_a"})

    assert [s.session_id for s in sessions] == ["sess_b"]


@pytest.mark.asyncio
async def test_caps_at_max_sessions():
    rows = []
    for sid in ("s0", "s1", "s2"):
        for r, (role, content) in enumerate(_rounds(2)):
            rows.append(_row(role, content, sid, ts=r))

    source, _ = _make_source(rows, min_rounds=2, max_sessions=2)
    sessions = await source.iter_new_sessions(set())

    assert len(sessions) == 2
    assert [s.session_id for s in sessions] == ["s0", "s1"]


@pytest.mark.asyncio
async def test_coerces_list_content_to_text():
    rows = [
        _row("user", [{"type": "text", "text": "part1"}, "part2"], "sess_a", ts=0),
        _row("assistant", "ok", "sess_a", ts=1),
        _row("user", "again", "sess_a", ts=2),
        _row("assistant", "sure", "sess_a", ts=3),
    ]
    source, _ = _make_source(rows, min_rounds=2)
    sessions = await source.iter_new_sessions(set())

    assert sessions[0].events[0] == {"role": "user", "content": "part1\npart2"}


@pytest.mark.asyncio
async def test_empty_store_returns_empty():
    source, _ = _make_source([], min_rounds=2)
    assert await source.iter_new_sessions(set()) == []
