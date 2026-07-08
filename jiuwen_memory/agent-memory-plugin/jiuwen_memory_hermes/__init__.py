# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwen-memory memory provider for Hermes Agent.

Drop this folder into ``~/.hermes/plugins/jiuwen_memory/`` (the folder name is
arbitrary — Hermes loads plugins by path; the canonical name lives in
``plugin.yaml``) or install via ``hermes plugin install jiuwen_memory``.

Requires a running ``jiuwen_memory`` memory_server::

    python -m jiuwen_memory.server.memory_server     # default 127.0.0.1:8000

The plugin is **self-contained**: it talks to the memory_server HTTP API with
only the Python standard library (``urllib`` + ``threading``). It deliberately
does *not* import the ``jiuwen_memory`` SDK — the SDK provider
(``JiuwenMemoryProvider``) is async and built for the in-process
``ExternalMemoryRail``; Hermes hooks are synchronous, so a thin sync HTTP
adapter is both simpler and safer inside the host process.

Identity mapping (Hermes → jiuwen):

- ``scope_id`` ← the project ``cwd`` Hermes passes to ``initialize()``. This
  isolates memories per project and avoids the cross-scope pollution the
  engine was hardened against.
- ``user_id`` ← ``JIUWEN_USER_ID`` env var, defaulting to ``hermes-user``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Framework glue (MemoryProvider ABC, env bootstrapping, auth guard, HTTP
# client) lives in _compat.py — keeping this file focused on the provider
# implementation itself.
from ._compat import (
    AuthGuard,
    MemoryProvider,
    ServerClient,
    preload_env,
    is_valid_url,
)

# --------------------------------------------------------------------------- #
# Defaults / env
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_USER_ID = "hermes-user"
DEFAULT_SCOPE_ID = "hermes-scope"

# memory_server endpoints. They are root-mounted (no ``/agentmemory/`` style
# prefix) and declared with a trailing slash in FastAPI, so we keep the slash
# to avoid a 307 redirect round-trip on every call.
_ADD_MESSAGES = "add_messages/"
_SEARCH_MEMORY = "search_memory/"
_SEARCH_SUMMARY = "search_user_history_summary/"
_HEALTH = "health"

DEFAULT_RECALL_MEM_NUM = 5
DEFAULT_RECALL_SUMMARY_NUM = 5
DEFAULT_THRESHOLD = 0.3


@dataclass(frozen=True)
class SearchRequest:
    """Named container for memory-search parameters passed to the server."""
    path: str
    query: str
    num: int
    user_id: str
    scope_id: str
    threshold: float = DEFAULT_THRESHOLD


# Bootstrap env at import time — guarantees JIUWEN_MEMORY_URL is set for
# ``hermes memory status``.
preload_env(DEFAULT_BASE_URL)


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def _results(payload: dict | None) -> list[dict]:
    """Extract the ``results`` list from an API response payload."""
    if not payload or not isinstance(payload, dict):
        return []
    items = payload.get("results", [])
    return items if isinstance(items, list) else []


def _format_recall(mem_results: list[dict], summary_results: list[dict]) -> str:
    """Render combined search results into one context string."""
    lines: list[str] = []

    def _line(r: dict, with_type: bool) -> str:
        content = str(r.get("content", "") or "").strip()
        score = r.get("score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        label = f"[{r.get('type', 'memory')}]" if with_type and r.get("type") else None
        head = f"{label} " if label else ""
        return f"- {head}{content[:300]} (score: {score:.2f})"

    if mem_results:
        lines.append("## Related Memories")
        lines.extend(_line(r, with_type=True) for r in mem_results)
    if summary_results:
        if lines:
            lines.append("")
        lines.append("## Related History Summaries")
        lines.extend(_line(r, with_type=False) for r in summary_results)
    return "\n".join(lines) if lines else ""


def _coerce_text(content: Any) -> str:
    """Flatten an Anthropic/Hermes message ``content`` field to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return " ".join(p for p in parts if p)
    return str(content)


# --------------------------------------------------------------------------- #
# Tool schemas + system prompt (jiuwen-specific, shared with the SDK provider)
# --------------------------------------------------------------------------- #
LTM_SEARCH_SCHEMA = {
    "name": "ltm_search",
    "description": (
        "Semantic search across long-term memory (user profile, episodic, semantic). "
        "Use whenever you need to recall what you know about a user or topic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num": {"type": "integer", "description": "Max results", "default": DEFAULT_RECALL_MEM_NUM},
            "threshold": {"type": "number", "description": "Min relevance (0-1)", "default": DEFAULT_THRESHOLD},
        },
        "required": ["query"],
    },
}

LTM_SEARCH_SUMMARY_SCHEMA = {
    "name": "ltm_search_summary",
    "description": (
        "Search past conversation summaries — higher-level than individual memories; "
        "captures whole conversations (topics discussed and conclusions reached). "
        "Call together with ltm_search for the fullest recall."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num": {"type": "integer", "description": "Max results", "default": DEFAULT_RECALL_SUMMARY_NUM},
        },
        "required": ["query"],
    },
}

_SYSTEM_PROMPT_BLOCK = (
    "# Long-Term Memory System\n\n"
    "You have long-term memory capabilities (jiuwen-memory) and can remember user "
    "information across sessions. The system automatically extracts valuable "
    "information from conversations and stores it in memory.\n\n"
    "## Memory Search\n\n"
    "When you need to recall previous information, use the `ltm_search` tool to "
    "search long-term memory, and `ltm_search_summary` for past conversation "
    "summaries. Search queries should contain key information (names, dates, event "
    "keywords). If results are insufficient, try again with different keywords.\n\n"
    "## Automatic Memory\n\n"
    "The system automatically extracts from each conversation: user profile "
    "(identity, preferences, habits), episodic memory (events, decisions), semantic "
    "memory (background knowledge), and conversation summaries."
)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class JiuwenHermesProvider(MemoryProvider):
    """Hermes memory provider backed by a remote jiuwen ``memory_server``."""

    @property
    def name(self) -> str:
        return "jiuwen_memory"

    # -- lifecycle ---------------------------------------------------------- #
    def is_available(self) -> bool:
        # Hermes contract: no network calls here — only check that the URL is
        # syntactically valid.
        return is_valid_url(_env_or("JIUWEN_MEMORY_URL", DEFAULT_BASE_URL))

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        base = _env_or("JIUWEN_MEMORY_URL", DEFAULT_BASE_URL)
        api_key = _env_or("JIUWEN_MEMORY_API_KEY", "")
        guard = AuthGuard()
        self._client = ServerClient(base, api_key=api_key, guard=guard)
        self._session_id = session_id
        # Per-project scope: use a fixed safe identifier to avoid Windows path
        # separators leaking into the vector-store collection name.
        self._scope_id = kwargs.get("scope_id") or DEFAULT_SCOPE_ID
        self._user_id = kwargs.get("user_id") or _env_or("JIUWEN_USER_ID", DEFAULT_USER_ID)
        if os.environ.get("JIUWEN_MEMORY_REQUIRE_HTTPS") == "1":
            guard.check(base, api_key)

        # Best-effort liveness check — never fatal; the server may come up later.
        health = self._client.call(_HEALTH, method="GET")
        if health and health.get("status") == "healthy":
            pass  # reachable; nothing else to do — jiuwen has no session/start endpoint

    def get_config_schema(self) -> list[dict]:
        return [
            {
                "key": "url",
                "description": "jiuwen memory_server URL",
                "default": DEFAULT_BASE_URL,
                "env_var": "JIUWEN_MEMORY_URL",
            },
            {
                "key": "api_key",
                "description": "jiuwen memory_server auth key (optional)",
                "secret": True,
                "required": False,
                "env_var": "JIUWEN_MEMORY_API_KEY",
            },
            {
                "key": "user_id",
                "description": "User identity to store memories under",
                "default": DEFAULT_USER_ID,
                "env_var": "JIUWEN_USER_ID",
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        config_path = Path(hermes_home) / "jiuwen_memory.json"
        config_path.write_text(json.dumps(values, indent=2))

    # -- prompt / prefetch -------------------------------------------------- #
    def system_prompt_block(self) -> str:
        # jiuwen has no dynamic /context endpoint, so a static guide is enough.
        return _SYSTEM_PROMPT_BLOCK

    def prefetch(self, query: str, **kwargs: Any) -> str:
        if not query:
            return ""
        user_id = kwargs.get("user_id", self._user_id)
        scope_id = kwargs.get("scope_id", self._scope_id)
        mem = self._do_search(SearchRequest(_SEARCH_MEMORY, query, DEFAULT_RECALL_MEM_NUM, user_id, scope_id))
        summary = self._do_search(SearchRequest(_SEARCH_SUMMARY, query, DEFAULT_RECALL_SUMMARY_NUM, user_id, scope_id))
        return _format_recall(mem, summary)

    def queue_prefetch(self, query: str, **kwargs: Any) -> None:
        # jiuwen has no cache to warm; Hermes reads the real result from prefetch().
        # Implemented as a no-op to satisfy the hook surface without pointless load.
        return None

    # -- tools -------------------------------------------------------------- #
    def get_tool_schemas(self) -> list[dict]:
        return [LTM_SEARCH_SCHEMA, LTM_SEARCH_SUMMARY_SCHEMA]

    def handle_tool_call(self, name: str, args: dict) -> str:
        # Hermes stores the return value as the tool-result content in session
        # history; Anthropic-protocol providers reject non-string content with a
        # 400, so always serialize to a JSON string.
        user_id = args.get("user_id", self._user_id)
        scope_id = args.get("scope_id", self._scope_id)

        if name == "ltm_search":
            results = self._do_search(SearchRequest(
                path=_SEARCH_MEMORY, query=args.get("query", ""),
                num=args.get("num", DEFAULT_RECALL_MEM_NUM),
                user_id=user_id, scope_id=scope_id,
                threshold=args.get("threshold", DEFAULT_THRESHOLD),
            ))
            return json.dumps({"results": results, "count": len(results)})

        if name == "ltm_search_summary":
            results = self._do_search(SearchRequest(
                path=_SEARCH_SUMMARY, query=args.get("query", ""),
                num=args.get("num", DEFAULT_RECALL_SUMMARY_NUM),
                user_id=user_id, scope_id=scope_id,
            ))
            return json.dumps({"results": results, "count": len(results)})

        return json.dumps({"error": f"Unknown tool: {name}"})

    # -- turn / session hooks ----------------------------------------------- #
    def sync_turn(self, user: str, assistant: str, **kwargs: Any) -> None:
        # Background write — must never block the LLM turn loop.
        turn_messages = _build_turn_payload(user, assistant)
        if not turn_messages:
            return
        self._send_turn_bg(turn_messages, **kwargs)

    def on_session_end(self, messages: list, **kwargs: Any) -> None:
        # jiuwen captures every turn via sync_turn() and extracts memories
        # incrementally, so there is no bulk "session dump" to do here. Kept as
        # a no-op to honour the hook surface (and as a future catch-up point if
        # sync_turn is ever disabled).
        return None

    def on_pre_compress(self, messages: list, **kwargs: Any) -> None:
        # Re-inject relevant memory before compaction so it survives the trim.
        query = _extract_last_user_text(messages)
        context = self.prefetch(query, **kwargs)
        if context and isinstance(messages, list):
            messages.insert(0, {
                "role": "user",
                "content": f"[jiuwen-memory context before compaction]\n{context}",
            })

    def on_memory_write(self, action: str, target: str, content: str, **kwargs: Any) -> None:
        # Mirror Hermes MEMORY.md writes into jiuwen as a user message so the
        # engine extracts/consolidates them like any other input.
        if action in ("add", "update") and content:
            self._send_turn_bg([{"role": "user", "content": content}], **kwargs)

    def shutdown(self, **kwargs: Any) -> None:
        pass

    # -- internals ---------------------------------------------------------- #
    def _do_search(self, req: SearchRequest) -> list[dict]:
        payload = self._client.call(req.path, {
            "query": req.query,
            "num": req.num,
            "user_id": req.user_id,
            "scope_id": req.scope_id,
            "threshold": req.threshold,
        })
        return _results(payload)

    def _send_turn_bg(self, messages: list[dict], **kwargs: Any) -> None:
        body = {
            "messages": messages,
            "user_id": kwargs.get("user_id", self._user_id),
            "scope_id": kwargs.get("scope_id", self._scope_id),
        }
        self._client.call_bg(_ADD_MESSAGES, body)


# --------------------------------------------------------------------------- #
# Pure helper functions (no overlap with any external plugin)
# --------------------------------------------------------------------------- #


def _env_or(key: str, fallback: str) -> str:
    """Return ``os.environ[key]`` if set, otherwise *fallback*."""
    return os.environ.get(key, fallback)


def _build_turn_payload(user: str, assistant: str) -> list[dict]:
    """Construct the message list for a sync_turn write.

    Separated from the provider method so the hook body stays short and the
    payload assembly is testable independently.
    """
    messages: list[dict] = []
    if user:
        messages.append({"role": "user", "content": user})
    if assistant:
        messages.append({"role": "assistant", "content": assistant})
    return messages


def _extract_last_user_text(messages: list) -> str:
    """Find the most recent user-role message text (up to 500 chars).

    Falls back to the last message of any role if no user message is found.
    """
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _coerce_text(msg.get("content"))[:500]
    # Fall back to the most recent message of any role.
    for msg in reversed(messages):
        if isinstance(msg, dict):
            return _coerce_text(msg.get("content"))[:500]
    return ""


# --------------------------------------------------------------------------- #
# Hermes plugin entry point
# --------------------------------------------------------------------------- #
def register(ctx: Any) -> None:
    ctx.register_memory_provider(JiuwenHermesProvider())


__all__ = ["JiuwenHermesProvider", "register"]
