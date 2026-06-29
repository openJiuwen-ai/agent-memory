# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session sources for dreaming.

A ``SessionSource`` answers "where does the conversation data come from".
The only phase-1 implementation, ``MessageStoreSessionSource``, consumes the
``LongTermMemory`` message_store (the structured store written by
``add_messages``) — no file formats, no jiuwenswarm dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Protocol, Set, Union, runtime_checkable

from jiuwen_memory.memory_core.manage.mem_model.message_manager import MessageManager

# message_store has no distinct / no time-window filter (SqlMessageStore.get_messages only
# supports user_id/scope_id/session_id). "Enumerate sessions" therefore means pulling all
# rows and grouping in memory — the same full-pull trick count_messages uses (limit=1_000_000).
PULL_LIMIT = 1_000_000


@dataclass
class NormalizedSession:
    session_id: str
    events: List[dict] = field(default_factory=list)   # [{"role": "user"|"assistant", "content": str}, ...]


@runtime_checkable
class SessionSource(Protocol):
    async def iter_new_sessions(self, scanned: Set[str]) -> List[NormalizedSession]:
        ...


def _as_text(content: Union[str, List[Any]]) -> str:
    """Coerce a BaseMessage.content (str | list[str|dict]) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # best-effort: prefer a "text"/"content" field, else stringify
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)


def _count_rounds(events: List[dict]) -> int:
    """A round == one user turn. Counts messages whose role is 'user'."""
    return sum(1 for e in events if str(e.get("role", "")).lower() == "user")


class MessageStoreSessionSource:
    """
    The only phase-1 source: consume LongTermMemory's message_store.

    Note: the caller must pass meaningful ``session_id`` values into
    ``add_messages``. If every turn uses the default ``"__default__"``, all
    messages collapse into one giant "session" and round/cap filters lose
    meaning. Phase 1 does not special-case the single-default-session case.
    """

    def __init__(
        self,
        message_manager: MessageManager,
        user_id: str,
        scope_id: str,
        min_rounds: int,
        max_sessions: int,
    ) -> None:
        self._message_manager = message_manager
        self._user_id = user_id
        self._scope_id = scope_id
        self._min_rounds = min_rounds
        self._max_sessions = max_sessions

    async def iter_new_sessions(self, scanned: Set[str]) -> List[NormalizedSession]:
        # 1) full pull (asc by timestamp) for this (user, scope)
        rows = await self._message_manager.store.get_messages(
            {"user_id": self._user_id, "scope_id": self._scope_id},
            limit=PULL_LIMIT,
            order_direction="asc",
        )

        # 2) group by session_id, preserving chronological first-seen order
        grouped: dict[str, List[dict]] = {}
        for message, metadata in rows:
            sid = metadata.session_id
            grouped.setdefault(sid, []).append(
                {"role": message.role, "content": _as_text(message.content)}
            )

        # 3) skip already-processed sessions; 4) drop short ones; cap at max_sessions
        result: List[NormalizedSession] = []
        for sid, events in grouped.items():
            if sid in scanned:
                continue
            if _count_rounds(events) < self._min_rounds:
                continue
            result.append(NormalizedSession(session_id=sid, events=events))
            if len(result) >= self._max_sessions:
                break
        return result
