# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jiuwen Memory MCP Server — long-term memory focused.

A Model Context Protocol server dedicated to long-term memory (profile,
semantic, episodic, summary) CRUD. It does **not** expose variable management
tools — variables are managed via ``memory_server`` REST API instead.

Any MCP-compatible client (Claude Code, Codex, Cursor, VS Code, …) can save,
search, update and delete memories through these tools.

The server builds a local ``LongTermMemory`` engine **in-process** via the
``jiuwen_memory`` SDK — the same KV / DB / Vector / embedding assembly that
``memory_server`` uses on startup. No separate HTTP service is required; the
MCP process owns the engine directly.

The engine is initialized lazily on the first tool call, and if it cannot be
built (missing deps, bad ``.env``, …) the server stays up and each tool
returns a readable error instead of crashing — the same pattern mem0's
``get_memory_client_safe`` uses.

Transport (selected via ``MCP_TRANSPORT`` env var):

- ``http`` / ``streamable-http`` (default): a persistent Streamable HTTP service
  (MCP spec 2025-03-26). Start it once; clients connect by URL::

      python -m jiuwen_memory.server.mcp_server
      # endpoint: http://127.0.0.1:8765/mcp

- ``sse``: a persistent SSE service; clients connect at ``http://<host>:<port>/sse``.

``MCP_HOST`` (default ``127.0.0.1``) and ``MCP_PORT`` (default ``8765``) set
the bind address.

Configuration is read from the same ``.env`` chain as ``memory_server``
(``~/.jiuwenmemory/.env`` → ``./.env``); see ``server/.env.example``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from jiuwen_memory.common.logging import memory_logger

# Matches ``LongTermMemory.DEFAULT_VALUE`` — duplicated as a literal so this
# module stays importable even before the (heavier) engine deps are installed.
_DEFAULT_ID = "__default__"

# --------------------------------------------------------------------------- #
# .env loading — mirrors memory_server._load_env (kept inline to avoid pulling
# in FastAPI app creation as an import side effect).
# --------------------------------------------------------------------------- #
_JIUWEN_DIR = Path.home() / ".jiuwenmemory"


def _load_env() -> None:
    """Load .env with the same priority chain as ``memory_server``."""
    home_env = _JIUWEN_DIR / ".env"
    if home_env.exists():
        load_dotenv(dotenv_path=str(home_env), override=True)
        return
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(dotenv_path=str(cwd_env), override=True)
        return
    _JIUWEN_DIR.mkdir(parents=True, exist_ok=True)
    memory_logger.warning(
        "No .env found in %s or the current directory. "
        "Create one (a template lives in server/.env.example).",
        str(_JIUWEN_DIR),
    )


_load_env()

# Optional per-deployment identity defaults; tools still accept overrides.
DEFAULT_USER_ID = os.getenv("MCP_DEFAULT_USER_ID", _DEFAULT_ID)
DEFAULT_SCOPE_ID = os.getenv("MCP_DEFAULT_SCOPE_ID", _DEFAULT_ID)


def _sanitize_id(value: str) -> str:
    """Sanitize id so it passes underlying validation (non-empty, no ``/``, ≤ 128)."""
    if not value:
        return _DEFAULT_ID
    return value.strip().replace("\\", "_").replace("/", "_")[:128]


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _dump(obj: Any) -> Any:
    """Best-effort JSON-friendly conversion of engine result objects."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dump(x) for x in obj]
    if hasattr(obj, "model_dump"):  # pydantic v2 (MemInfo / MemResult / units)
        try:
            return obj.model_dump(mode="json")
        except Exception as err:
            memory_logger.debug("[mcp] model_dump failed, falling back to str(): %s", err)
    return str(obj)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# In-process LongTermMemory engine
# --------------------------------------------------------------------------- #
class _Engine:
    """Owns the in-process ``LongTermMemory`` engine.

    Assembled exactly like ``memory_server`` startup: stores via
    ``store_factory`` + an ``APIEmbedding`` + a ``MemoryEngineConfig`` built
    from the same ``.env`` keys.
    """

    def __init__(self) -> None:
        self._ltm: Any = None
        self._ready = False
        # Stashed type references, populated in ``initialize()``. Declared here
        # so instance attributes are not defined outside __init__.
        self._base_message_cls: Any = None
        self._memory_type_cls: Any = None

    @property
    def is_ready(self) -> bool:
        return self._ready and self._ltm is not None

    async def initialize(self) -> None:
        # Lazy imports — keeps the module importable when optional deps are absent.
        from jiuwen_memory.foundation.llm import BaseMessage
        from jiuwen_memory.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
        from jiuwen_memory.memory_core.config.config import MemoryEngineConfig
        from jiuwen_memory.memory_core.long_term_memory import LongTermMemory
        from jiuwen_memory.memory_core.manage.mem_model.memory_unit import MemoryType
        from jiuwen_memory.retrieval.common.config import EmbeddingConfig
        from jiuwen_memory.retrieval.embedding.api_embedding import APIEmbedding
        from jiuwen_memory.server.store_factory import (
            create_async_engine_from_env,
            create_db_store,
            create_kv_store,
            create_vector_store,
        )

        engine = create_async_engine_from_env()
        kv_store = create_kv_store(engine)
        db_store = create_db_store(engine)
        vector_store = create_vector_store()
        embedding_model = APIEmbedding(
            config=EmbeddingConfig(
                model_name=os.getenv("EMBED_MODEL_NAME", ""),
                api_key=os.getenv("EMBED_API_KEY", ""),
                base_url=os.getenv("EMBED_API_BASE", ""),
            )
        )

        self._ltm = LongTermMemory()
        if self._ltm.kv_store is None:
            await self._ltm.register_store(
                kv_store=kv_store,
                db_store=db_store,
                vector_store=vector_store,
                embedding_model=embedding_model,
            )

        self._ltm.set_config(
            MemoryEngineConfig(
                default_model_cfg=ModelRequestConfig(model=os.getenv("MODEL_NAME", "")),
                default_model_client_cfg=ModelClientConfig(
                    client_provider=os.getenv("MODEL_PROVIDER", ""),
                    api_key=os.getenv("API_KEY", ""),
                    api_base=os.getenv("API_BASE", ""),
                    # 是否校验 LLM API 的 TLS 证书；默认不校验，设为 true 时开启
                    verify_ssl=os.getenv("MODEL_SSL_VERIFY", "false").strip().lower() == "true",
                ),
                # 是否启用中期记忆；默认不启用，设为 true 时开启
                enable_middle_memory=os.getenv("MEMORY_ENABLE_MIDDLE_MEMORY", "false").strip().lower() == "true",
            )
        )
        # Stash the few types we need at call time.
        self._base_message_cls = BaseMessage
        self._memory_type_cls = MemoryType
        self._ready = True
        memory_logger.info("[mcp] LongTermMemory engine initialized")

    async def add_messages(self, messages: list[dict], user_id: str, scope_id: str, infer: bool) -> dict:
        from jiuwen_memory.memory_core.config.config import AgentMemoryConfig

        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        base_messages = [
            self._base_message_cls(role=m.get("role", "user"), content=m.get("content", ""))
            for m in messages
        ]
        result = await self._ltm.add_messages(
            messages=base_messages,
            agent_config=AgentMemoryConfig(),
            user_id=user_id,
            scope_id=scope_id,
            gen_mem=infer,
        )
        return {
            "status": "added",
            "infer": infer,
            "user_profile": _dump(result.user_profile),
            "semantic_memory": _dump(result.semantic_memory),
            "episodic_memory": _dump(result.episodic_memory),
            "summary": _dump(result.summary),
            "variables": _dump(result.variables),
        }

    async def search_memories(self, query: str, num: int, user_id: str, scope_id: str, threshold: float) -> list[dict]:
        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        results = await self._ltm.search_user_mem(
            query=query, num=num, user_id=user_id, scope_id=scope_id, threshold=threshold,
        )
        return [
            {
                "mem_id": r.mem_info.mem_id,
                "content": r.mem_info.content,
                "type": r.mem_info.type.value if r.mem_info.type else "unknown",
                "score": r.score,
            }
            for r in results
        ]

    async def search_history_summaries(
        self, query: str, num: int, user_id: str, scope_id: str, threshold: float,
    ) -> list[dict]:
        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        results = await self._ltm.search_user_history_summary(
            query=query, num=num, user_id=user_id, scope_id=scope_id, threshold=threshold,
        )
        return [
            {
                "mem_id": r.mem_info.mem_id,
                "content": r.mem_info.content,
                "type": r.mem_info.type.value if r.mem_info.type else "unknown",
                "score": r.score,
            }
            for r in results
        ]

    async def get_memories(
        self, page_size: int, page_idx: int, memory_type: str, user_id: str, scope_id: str,
    ) -> list[dict]:
        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        try:
            mem_type = self._memory_type_cls(memory_type.lower())
        except ValueError:
            mem_type = self._memory_type_cls.UNKNOWN
        results = await self._ltm.get_user_mem_by_page(
            user_id=user_id, scope_id=scope_id,
            page_size=page_size, page_idx=page_idx, memory_type=mem_type,
        )
        return [
            {
                "mem_id": r.mem_id,
                "content": r.content,
                "type": r.type.value if r.type else "unknown",
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in results
        ]

    async def update_memory(self, mem_id: str, memory: str, user_id: str, scope_id: str) -> dict:
        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        await self._ltm.update_mem_by_id(mem_id=mem_id, memory=memory, user_id=user_id, scope_id=scope_id)
        return {"status": "updated", "mem_id": mem_id}

    async def delete_memory(self, mem_id: str, user_id: str, scope_id: str) -> dict:
        user_id, scope_id = _sanitize_id(user_id), _sanitize_id(scope_id)
        await self._ltm.delete_mem_by_id(mem_id=mem_id, user_id=user_id, scope_id=scope_id)
        return {"status": "deleted", "mem_id": mem_id}

    async def delete_all_memories(self, scope_id: str) -> dict:
        scope_id = _sanitize_id(scope_id)
        await self._ltm.delete_mem_by_scope(scope_id=scope_id)
        return {"status": "deleted", "scope_id": scope_id}

    async def shutdown(self) -> None:
        if self._ltm is not None and hasattr(self._ltm, "stop"):
            try:
                await self._ltm.stop()
            except Exception as stop_exc:
                memory_logger.debug("[mcp] engine stop failed: %s", stop_exc)
        self._ready = False
        self._ltm = None


# --------------------------------------------------------------------------- #
# Lazy, resilient engine accessor
# --------------------------------------------------------------------------- #
_engine: _Engine | None = None
_init_lock = asyncio.Lock()


async def _get_engine() -> _Engine:
    """Return the ready engine, (re)initializing lazily on first use or after failure."""
    global _engine
    if _engine is not None and _engine.is_ready:
        return _engine
    async with _init_lock:
        if _engine is None:
            _engine = _Engine()
        if not _engine.is_ready:
            await _engine.initialize()
    return _engine


def reset_engine() -> None:
    """Clear the cached engine so it is rebuilt on the next tool call.

    Intended for tests that re-wire the engine's dependencies (the
    ``LongTermMemory`` source, the ``store_factory`` functions) and need a
    fresh build for isolation. Safe in production — the engine is simply
    rebuilt lazily on the next request.
    """
    global _engine
    _engine = None


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #
try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - optional, surfaced at run time
    raise RuntimeError(
        "The 'mcp' package is required for the MCP server. "
        "Install it with: pip install -e '.[server]'"
    ) from exc

mcp = FastMCP("jiuwen-memory-mcp-server")


def _tool_error(action: str, error: Exception) -> str:
    memory_logger.exception("[mcp] %s failed", action)
    return _json({"error": f"{action} failed: {error}"})


@mcp.tool(description=(
    "Add messages (each a {role, content} dict) to long-term memory. jiuwen has "
    "no plain message store — messages are always extracted into memories "
    "(profile / semantic / episodic / summary), so a single user message is just "
    '[{"role": "user", "content": "..."}]. Set infer=False to skip LLM extraction '
    "and ingest the batch raw."
))
async def add_messages(
    messages: list[dict],
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
    infer: bool = True,
) -> str:
    try:
        engine = await _get_engine()
        result = await engine.add_messages(
            messages=messages, user_id=user_id, scope_id=scope_id, infer=infer,
        )
        return _json(result)
    except Exception as err:
        return _tool_error("add_messages", err)


@mcp.tool(description=(
    "Semantic search across user memories (profile, semantic, episodic). Call this "
    "whenever you need to recall what you know about a user or topic — and pair it "
    "with search_history_summaries every time for the fullest context."
))
async def search_memories(
    query: str,
    num: int = 5,
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
    threshold: float = 0.3,
) -> str:
    try:
        engine = await _get_engine()
        results = await engine.search_memories(
            query=query, num=num, user_id=user_id, scope_id=scope_id, threshold=threshold,
        )
        return _json({"results": results, "count": len(results)})
    except Exception as err:
        return _tool_error("search_memories", err)


@mcp.tool(description=(
    "Search past conversation summaries — higher-level than individual memories, "
    "they capture whole conversations (topics discussed and conclusions reached). "
    "Call this TOGETHER with search_memories every time you need context: "
    "searching both gives the fullest recall of what you know about the user/topic."
))
async def search_history_summaries(
    query: str,
    num: int = 3,
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
    threshold: float = 0.3,
) -> str:
    try:
        engine = await _get_engine()
        results = await engine.search_history_summaries(
            query=query, num=num, user_id=user_id, scope_id=scope_id, threshold=threshold,
        )
        return _json({"results": results, "count": len(results)})
    except Exception as err:
        return _tool_error("search_history_summaries", err)


@mcp.tool(description=(
    "List memories page by page (newest first), optionally filtered by type. "
    "memory_type one of: unknown (all), user_profile, semantic_memory, "
    "episodic_memory, summary, variable, middle_term_memory."
))
async def get_memories(
    page_size: int = 10,
    page_idx: int = 1,
    memory_type: str = "unknown",
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
) -> str:
    try:
        engine = await _get_engine()
        results = await engine.get_memories(
            page_size=page_size, page_idx=page_idx, memory_type=memory_type,
            user_id=user_id, scope_id=scope_id,
        )
        return _json({"results": results, "count": len(results), "page_idx": page_idx})
    except Exception as err:
        return _tool_error("get_memories", err)


@mcp.tool(description="Overwrite a memory's text by its mem_id.")
async def update_memory(
    mem_id: str,
    memory: str,
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
) -> str:
    try:
        engine = await _get_engine()
        result = await engine.update_memory(mem_id=mem_id, memory=memory, user_id=user_id, scope_id=scope_id)
        return _json(result)
    except Exception as err:
        return _tool_error("update_memory", err)


@mcp.tool(description="Delete a single memory by its mem_id.")
async def delete_memory(
    mem_id: str,
    user_id: str = DEFAULT_USER_ID,
    scope_id: str = DEFAULT_SCOPE_ID,
) -> str:
    try:
        engine = await _get_engine()
        result = await engine.delete_memory(mem_id=mem_id, user_id=user_id, scope_id=scope_id)
        return _json(result)
    except Exception as err:
        return _tool_error("delete_memory", err)


@mcp.tool(description=(
    "DANGEROUS & IRREVERSIBLE: delete ALL memories (every type) within a scope. "
    "The caller MUST pass confirm=true to proceed; any other value is rejected "
    "so an accidental call does nothing."
))
async def delete_all_memories(
    scope_id: str = DEFAULT_SCOPE_ID,
    confirm: bool = False,
) -> str:
    if not confirm:
        return _json({
            "status": "aborted",
            "reason": "confirm must be true to delete all memories in a scope "
                      "(this action is irreversible).",
            "scope_id": scope_id,
        })
    try:
        engine = await _get_engine()
        result = await engine.delete_all_memories(scope_id=scope_id)
        return _json(result)
    except Exception as err:
        return _tool_error("delete_all_memories", err)


@mcp.tool(description="Report engine readiness — useful for diagnosing init failures.")
async def health_check() -> str:
    try:
        engine = await _get_engine()
        return _json({"status": "healthy" if engine.is_ready else "initializing", "ready": engine.is_ready})
    except Exception as err:
        return _json({"status": "unavailable", "error": str(err)})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the MCP server as a persistent HTTP/SSE service.

    Start it once; MCP clients (Claude Code, Codex, Cursor, …) connect by URL.
    The transport is selected via ``MCP_TRANSPORT``:

    - ``http`` / ``streamable-http`` (default): Streamable HTTP (MCP spec
      2025-03-26). Clients connect at ``http://<host>:<port>/mcp``.
    - ``sse``: SSE transport. Clients connect at ``http://<host>:<port>/sse``.

    ``MCP_HOST`` (default ``127.0.0.1``) and ``MCP_PORT`` (default ``8765``)
    set the bind address.
    """
    transport = os.getenv("MCP_TRANSPORT", "http").strip().lower()

    if transport in ("http", "streamable-http", "sse"):
        try:
            import uvicorn
        except ImportError as imp_err:  # pragma: no cover
            raise RuntimeError(
                "uvicorn is required for http/sse transport. "
                "Install with: pip install -e '.[server]'"
            ) from imp_err

        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", "8765"))
        is_sse = transport == "sse"
        app = mcp.sse_app() if is_sse else mcp.streamable_http_app()
        endpoint = "/sse" if is_sse else "/mcp"
        memory_logger.info(
            "[mcp] starting jiuwen-memory-mcp-server (%s) at http://%s:%d%s",
            transport, host, port, endpoint,
        )
        uvicorn.run(app, host=host, port=port)
        return

    raise ValueError(
        f"Unsupported MCP_TRANSPORT={transport!r}, expected 'http' | 'sse'."
    )


if __name__ == "__main__":
    main()


__all__ = ["mcp", "main", "reset_engine"]
