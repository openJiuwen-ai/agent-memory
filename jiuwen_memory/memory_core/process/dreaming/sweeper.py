# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Sweeper: the dreaming pipeline.

Pulls new sessions from a ``SessionSource``, compresses each within a token
budget, asks the LLM to extract cross-session knowledge, and promotes the
results through a ``KnowledgeStore``. Carries no format knowledge of its own.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List

from jiuwen_memory.foundation.llm import JsonOutputParser
from jiuwen_memory.memory_core.config.config import DreamingConfig
from jiuwen_memory.memory_core.process.dreaming.source import NormalizedSession, SessionSource
from jiuwen_memory.memory_core.process.dreaming.store import KnowledgeItem, MemoryUnitKnowledgeStore
from jiuwen_memory.memory_core.prompts.prompt_applier import PromptApplier
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType

_EXTRACT_RETRIES = 3


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token); good enough for budgeting."""
    return max(1, len(text) // 4)


def _events_tokens(events: List[dict]) -> int:
    return sum(_estimate_tokens(str(e.get("role", "")) + str(e.get("content", ""))) for e in events)


def _compress(events: List[dict], max_tokens: int) -> List[dict]:
    """
    Trim events to fit the token budget by dropping from the middle,
    keeping the oldest and newest context. Always converges.
    """
    events = list(events)
    while len(events) > 2 and _events_tokens(events) > max_tokens:
        events.pop(len(events) // 2)
    return events


def _format_dialogue(events: List[dict]) -> str:
    return "\n".join(f"{e.get('role', '')}: {e.get('content', '')}" for e in events)


class Sweeper:
    def __init__(
        self,
        source: SessionSource,
        store: MemoryUnitKnowledgeStore,
        llm,
        config: DreamingConfig,
        checkpoint_io,
        user_id: str,
        scope_id: str,
        important_memory_definition: str = "",
    ) -> None:
        self._source = source
        self._store = store
        self._llm = llm
        self._config = config
        self._kv = checkpoint_io
        self._user_id = user_id
        self._scope_id = scope_id
        self._important_memory_definition = important_memory_definition
        self._ckpt_key = f"dreaming/checkpoint/{scope_id}/{user_id}"
        self._prompt_applier = PromptApplier()

    async def run_sweep(self) -> None:
        scanned = await self._load_checkpoint()
        sessions = await self._source.iter_new_sessions(scanned)
        if not sessions:
            return

        succeeded: list[str] = []
        all_items: list[KnowledgeItem] = []
        for session in sessions:
            try:
                compressed = _compress(session.events, self._config.max_compress_tokens)
                items = await self._extract(NormalizedSession(session.session_id, compressed))
                all_items.extend(items)
                succeeded.append(session.session_id)
            except Exception as exc:
                # failed session is NOT marked → retried next sweep
                memory_logger.warning(
                    "dreaming: extract failed for session %s, will retry: %s",
                    session.session_id, exc,
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=self._user_id,
                    scope_id=self._scope_id,
                )

        # promote first: if it raises, checkpoint is NOT advanced → batch is reswept next cycle
        if all_items:
            await self._store.promote(all_items)
        if succeeded:
            await self._save_checkpoint(scanned | set(succeeded))

    async def _extract(self, session: NormalizedSession) -> List[KnowledgeItem]:
        """
        LLM: dialogue → KnowledgeItem[]. Raises on persistent parse failure
        (so the session is retried); returns [] on a clean empty extraction.
        """
        dialogue = _format_dialogue(session.events)
        prompt = self._prompt_applier.apply(
            "dreaming_extraction",
            {
                "dialogue": dialogue,
                "max_items": str(self._config.max_items_per_session),
                "important_memory_definition": self._important_memory_definition,
            },
        )
        messages = [{"role": "user", "content": prompt}]
        parser = JsonOutputParser()

        last_exc: Exception | None = None
        for attempt in range(_EXTRACT_RETRIES):
            try:
                response = await self._llm.invoke(messages=messages)
                parsed = await parser.parse(response.content)
                if isinstance(parsed, dict):
                    parsed = [parsed]
                if not isinstance(parsed, list):
                    raise ValueError(f"expected JSON array, got {type(parsed).__name__}")
                items = []
                for obj in parsed[: self._config.max_items_per_session]:
                    # is_important: default False when the LLM omits the
                    # field or returns a non-bool. Strictly coerce —
                    # anything truthy that isn't an explicit bool True
                    # becomes False so we never accidentally protect a
                    # memory the LLM didn't actually flag.
                    raw_flag = obj.get("is_important", False)
                    is_important = raw_flag is True or raw_flag == 1 or (
                        isinstance(raw_flag, str) and raw_flag.strip().lower() == "true"
                    )
                    items.append(KnowledgeItem(
                        mem_type=str(obj.get("mem_type", "")),
                        content=str(obj.get("content", "")),
                        source_session_id=session.session_id,
                        is_important=is_important,
                    ))
                return items
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                last_exc = exc
                memory_logger.warning(
                    "dreaming: extract parse error (%s/%s): %s",
                    attempt + 1, _EXTRACT_RETRIES, exc,
                    event_type=LogEventType.MEMORY_PROCESS,
                    user_id=self._user_id,
                    scope_id=self._scope_id,
                )
        raise last_exc if last_exc else RuntimeError("extract failed")

    async def _load_checkpoint(self) -> set[str]:
        raw = await self._kv.get(self._ckpt_key)
        if not raw:
            return set()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return set()
        return set(data.get("scanned_sessions", []))

    async def _save_checkpoint(self, scanned: set[str]) -> None:
        data = {
            "scanned_sessions": sorted(scanned),
            "last_scan_ts": datetime.now(timezone.utc).astimezone().isoformat(),
        }
        await self._kv.set(self._ckpt_key, json.dumps(data))
