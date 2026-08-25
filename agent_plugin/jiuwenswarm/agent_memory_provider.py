# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentMemory 外接记忆 provider —— 适配 JiuwenSwarm ExternalMemoryRail。

把 AgentMemory 的 ``MemoryAPI``（进程内 ``assemble()`` 或 HTTP ``/v1/<verb>``）
适配成 openjiuwen ``MemoryProvider`` ABC 的 8 抽象方法实现，供
``ExternalMemoryRail(provider, user_id, scope_id)`` 驱动。

契约来源（openjiuwen 源码确认，见 ``docs/features/ExternalMemoryProvider契约-代码确认版.md``）：
- ABC：``openjiuwen/core/memory/external/provider.py`` 的 ``MemoryProvider``
- 参考实现：``openjiuwen/core/memory/external/mem0_provider.py``
- Rail 调用点：``openjiuwen/harness/rails/memory/external_memory_rail.py``

设计要点（见 ``docs/features/AgentMemory-JiuwenSwarm接入适配分析.md``）：
- 双模式：``base_url`` 非空 → HTTP（路径 B，全 async，推荐生产）；否则进程内 ``assemble()``（路径 A）
- ``prefetch`` 返 Markdown 字符串（Rail 包 ``<memory-context>`` 注入）
- ``handle_tool_call`` 返 JSON 字符串（Rail ``json.loads``）
- ``sync_turn`` 只 ``add`` 存原文，EXTRACT 推迟到 ``on_session_end`` 抑制每轮风暴（§4.1.2）
- 隔离轴：Rail 的 ``user_id``/``agent_id``/``session_id``/``scope_id`` → AgentMemory ``Scope``
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.memory.external.provider import MemoryProvider

from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX
from jiuwen_memory.common.type_def.memory_codec import loads

# [本地修改 2026-06-29] provider 通过 PYTHONPATH 以顶层模块 agent_memory_provider 被 import，
# __name__="agent_memory_provider" 不在 jiuwenswarm/openjiuwen 的 logger 树下，INFO 默认不落文件。
# 挂到 jiuwenswarm logger 树，让 prefetch 召回日志能进 agent_server.log / full.log。
logger = logging.getLogger("jiuwenswarm.agents.harness.common.memory.external.agent_memory_provider")

# --------------------------------------------------------------------------- #
# 工具 schema（OpenAI 风格，Rail 据此自动包 ToolCard，见 §4.8）
# --------------------------------------------------------------------------- #

PROFILE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_profile",
    "description": (
        "Retrieve stored memories about the user — preferences, facts, project context. "
        "Use at conversation start for a full overview."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_search",
    "description": "Search memories by meaning. Returns relevant memories ranked by similarity.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {
                "type": "integer",
                "description": "Max results (default: 10, max: 50).",
            },
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_conclude",
    "description": (
        "Store a durable fact about the user. Stored verbatim (no extraction). "
        "Use for explicit preferences, corrections, or decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact to store."}
        },
        "required": ["conclusion"],
    },
}

PROCEDURAL_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_procedural",
    "description": (
        "Store a structured procedural memory — a summary of what was done this turn "
        "(goal / steps / outcome). The raw conversation is NOT stored; the LLM summarizes "
        "it into ONE procedural memory record. Use to record how-to / execution history "
        "worth recalling later. No deduplication or context retrieval is performed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The conversation/turn content to summarize into a procedural memory.",
            }
        },
        "required": ["content"],
    },
}

_MAX_TOP_K = 50
_DEFAULT_PREFETCH_TOP_K = 5
_DEFAULT_SEARCH_TOP_K = 10


class AgentMemoryMemoryProvider(MemoryProvider):
    """AgentMemory 适配 ``MemoryProvider`` ABC。

    构造参数（与 jiuwenswarm ``memory.external.agent_memory`` 子段对齐，见 §8.1）：

    - ``base_url``：AgentMemory HTTP 服务地址（如 ``http://127.0.0.1:8137``）。
      非空 → HTTP 模式（路径 B，全 async，推荐生产）；空 → 进程内模式（路径 A）。
    - ``config_path``：进程内模式的 AgentMemory 配置文件路径（YAML/JSON）。
      生产应配真实后端 + llm extractor（见 §4.3）。
    - ``user_id``/``agent_id``：mem0 兼容隔离键（默认与 mem0 一致）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        config_path: str | None = None,
        user_id: str = "jiuwenswarm-user",
        agent_id: str = "jiuwenswarm",
        **_kwargs: Any,
    ) -> None:
        self._base_url = base_url or ""
        self._config_path = config_path
        self._user_id = user_id
        self._agent_id = agent_id

        # Rail 在 before_invoke 首次调 initialize 时传入，覆盖构造默认
        self._scope_id: str = "__default__"
        self._session_id: str = "__default__"

        self._client: _AgentMemoryClient | None = None
        self._initialized = False

    # -- MemoryProvider: 元信息 ---------------------------------------------- #

    @property
    def name(self) -> str:
        return "agent_memory"

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # -- MemoryProvider: 主动召回（每轮 before_model_call，5s 超时） ---------- #

    # jiuwenswarm 把用户输入包成 TUI 信封再送进 agent（见 interface.py:498）：
    #   "你收到一条消息：\n{source,timezone,timestamp,content,...json}"
    #   / "You receive a new message:\n{...}"
    # rail 的 _resolve_user_text_for_memory 老实取了整段信封当 query 传给 prefetch，
    # 导致 AgentMemory 用带壳文本检索，命中对话原文而非语义事实。
    # prefetch 前剥信封，只取纯 content 作为检索 query。
    _TUI_ENVELOPE_PREFIXES = (
        "你收到一条消息：\n",
        "你收到一条消息，对于查询类任务必须输出查询到的内容，不要只回复确认，不要记录到memory：\n",
        "You receive a new message:\n",
        "You receive a new message. For query tasks, you must output the queried content"
        "—don't just reply with confirmation, don't record to memory:\n",
    )

    @classmethod
    def _strip_tui_envelope(cls, query: str) -> str:
        """若 query 是 jiuwenswarm TUI 信封，剥出纯 content；否则原样返回。"""
        if not query:
            return query
        for prefix in cls._TUI_ENVELOPE_PREFIXES:
            if query.startswith(prefix):
                rest = query[len(prefix):].lstrip()
                try:
                    data = json.loads(rest)
                except json.JSONDecodeError:
                    # 前缀命中但非合法 json（可能 interaction_prefix 拼在前），回退原 query
                    return query
                if isinstance(data, dict):
                    content = data.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                return query
        return query

    def is_available(self) -> bool:
        """配置就绪探测（契约要求无网络调用）。

        HTTP 模式：``base_url`` 配置就绪即可；进程内模式：``config_path`` 或
        默认装配可用即可。不真发请求（避免网络）。
        """
        if self._base_url:
            return True
        return self._config_path is not None  # 进程内：有配置路径即视为可装配

    async def initialize(self, **kwargs: Any) -> None:
        """Rail ``before_invoke`` 首次调，传 ``user_id``/``scope_id``/``session_id``。"""
        self._user_id = kwargs.get("user_id") or self._user_id
        self._scope_id = kwargs.get("scope_id", "__default__")
        self._session_id = kwargs.get("session_id", "__default__")
        # mem0 兼容：agent_id 可由 kwargs 覆盖
        if kwargs.get("agent_id"):
            self._agent_id = kwargs["agent_id"]

        self._client = _build_client(self._base_url, self._config_path)
        self._initialized = True
        logger.info(
            "[AgentMemoryMemoryProvider] initialized (mode=%s, user=%s, scope=%s, session=%s)",
            "http" if self._base_url else "in-process",
            self._user_id, self._scope_id, self._session_id,
        )

    # -- MemoryProvider: 工具声明 + 调度 -------------------------------------- #

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI 风格 schema；Rail 自动包成 ToolCard（§4.8）。"""
        schemas = [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA, PROCEDURAL_SCHEMA]
        logger.info(
            "[AgentMemoryMemoryProvider] get_tool_schemas CALLED -> returning %d schemas: %s",
            len(schemas), [s.get("name") for s in schemas],
        )
        return schemas

    # Pylint: this dispatch keeps tool-specific state together for JSON responses.
    # pylint: disable=too-many-locals
    async def handle_tool_call(self, tool_name: str, args: dict) -> str:
        """LLM 调工具时触发；返 JSON 字符串（Rail 会 ``json.loads``）。"""
        logger.info("[AgentMemoryMemoryProvider] handle_tool_call CALLED tool=%s args=%s", tool_name, args)
        if self._client is None:
            return json.dumps({"error": "provider not initialized"})
        scope = self._scope()

        try:
            if tool_name == "agent_memory_profile":
                items = await self._client.list_semantic(scope)
                if not items:
                    logger.info("[AgentMemoryMemoryProvider] agent_memory_profile -> no memories")
                    return json.dumps({"result": "No memories stored yet."})
                lines = [it["content"] for it in items if it.get("content")]
                logger.info("[AgentMemoryMemoryProvider] agent_memory_profile -> count=%d", len(lines))
                for idx, line in enumerate(lines):
                    logger.info("[AgentMemoryMemoryProvider] profile hit[%d]: %s", idx, line[:300])
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})

            if tool_name == "agent_memory_search":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"error": "Missing required parameter: query"})
                top_k = min(int(args.get("top_k", _DEFAULT_SEARCH_TOP_K)), _MAX_TOP_K)
                items = await self._client.search(query, scope, top_k=top_k)
                if not items:
                    logger.info("[AgentMemoryMemoryProvider] agent_memory_search query=%r -> no relevant", query)
                    return json.dumps({"result": "No relevant memories found."})
                payload = [
                    {"memory": it.get("content", ""), "score": it.get("score", 0)}
                    for it in items
                ]
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_search query=%r -> count=%d",
                    query, len(payload),
                )
                for idx, p in enumerate(payload):
                    logger.info(
                        "[AgentMemoryMemoryProvider] search hit[%d] score=%s: %s",
                        idx, p.get("score"), str(p.get("memory"))[:300],
                    )
                return json.dumps({"results": payload, "count": len(payload)})

            if tool_name == "agent_memory_conclude":
                conclusion = args.get("conclusion", "")
                if not conclusion:
                    return json.dumps({"error": "Missing required parameter: conclusion"})
                # 原样存（对齐 mem0 infer=False）；add_async 会自动触发 background
                # EXTRACT，但默认占位空转（§4.1.1），需配 extractor:llm 才真抽取。
                await self._client.add(conclusion, scope, tags=["conclude"])
                logger.info("[AgentMemoryMemoryProvider] agent_memory_conclude stored=%r", conclusion[:300])
                return json.dumps({"result": "Fact stored."})

            if tool_name == "agent_memory_procedural":
                content = args.get("content", "")
                if not content:
                    return json.dumps({"error": "Missing required parameter: content"})
                # 过程记忆：经 add(procedural=true) 触发 engine 的 procedural 分支——
                # 原文不落 KV，evolver 让 extractor 把本轮汇总成 1 条 PROCEDURAL 执行历史，
                # 落 /memory/ 建索引；不走去重、不收集 context（见 F02「过程记忆抽取」）。
                # 需配 extractor:llm 才真汇总（默认 keyword 降级为原文原样存 1 条 PROCEDURAL）。
                item_id = await self._client.add(
                    content, scope, system_metadata={"procedural": "true"}
                )
                logger.info(
                    "[AgentMemoryMemoryProvider] agent_memory_procedural summarized=%r item_id=%s",
                    content[:300], item_id,
                )
                # add 返回 None：extractor 产空（LLM 返回不可解析/未产出候选）→ 未持久化任何记忆。
                # 不可报成功（false success）——evolver 吞掉 LLM 失败返回空 EvolveResult，
                # engine/handler 返回 item_id=None，需如实告知调用方。content 已在上文校验非空。
                if not item_id:
                    return json.dumps({
                        "error": "Procedural memory not stored: extractor produced nothing "
                                 "(LLM returned unparseable content or no candidates).",
                    })
                return json.dumps({"result": "Procedural memory stored.", "item_id": item_id})

            logger.info("[AgentMemoryMemoryProvider] handle_tool_call unknown tool=%s", tool_name)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            logger.warning("[AgentMemoryMemoryProvider] handle_tool_call '%s' failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)})

    async def prefetch(self, query: str, **kwargs: Any) -> str:
        """返 Markdown 字符串，Rail 包 ``<memory-context>`` 注入提示词。"""
        logger.info("[AgentMemoryMemoryProvider] prefetch CALLED query=%r client=%s", query, bool(self._client))
        if not query or self._client is None:
            return ""
        # [本地修改 2026-06-29] 剥 TUI 信封，用纯 content 检索，避免命中对话原文壳。
        search_query = self._strip_tui_envelope(query)
        if search_query != query:
            logger.info("[AgentMemoryMemoryProvider] prefetch stripped envelope: %r -> %r", query, search_query)
        top_k = min(int(kwargs.get("top_k", _DEFAULT_PREFETCH_TOP_K)), _MAX_TOP_K)
        try:
            logger.info("[AgentMemoryMemoryProvider] prefetch before search scope=%s top_k=%d", self._scope(), top_k)
            items = await self._client.search(search_query, self._scope(), top_k=top_k)
            logger.info("[AgentMemoryMemoryProvider] prefetch after search items=%d", len(items))
            lines = [it.get("content", "") for it in items if it.get("content")]
            # [本地修改 2026-06-29] 打印 prefetch 召回的记忆，便于排查外接记忆是否生效。
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch search_query=%r scope=%s top_k=%d recalled=%d",
                search_query, self._scope(), top_k, len(lines),
            )
            for idx, line in enumerate(lines):
                logger.info(
                    "[AgentMemoryMemoryProvider] prefetch hit[%d]: %s",
                    idx, line[:500],
                )
            return (
                "## AgentMemory Memory\n" + "\n".join(f"- {line}" for line in lines)
            ) if lines else ""
        except Exception as exc:
            logger.warning("[AgentMemoryMemoryProvider] prefetch failed: %s", exc, exc_info=True)
            return ""

    # -- MemoryProvider: 每轮回写（after_invoke，非心跳/cron） --------------- #

    async def sync_turn(
        self, user_msg: str, assistant_msg: str, **kwargs: Any
    ) -> None:
        """存本轮对话原文并**同步抽取事实**（对齐 mem0 ``add(infer=True)``）。

        传 ``system_metadata={"infer": "true"}`` 给 AgentMemory ``add``：hot path 同步调
        Extractor 从 ``user: ...\\nassistant: ...`` 抽取派生事实并建索引，**原文
        落 KV 真源但不建索引**，且**不再提交 background EXTRACT**（已同步抽取，
        避免每轮全量重扫+逐条 LLM 的 EXTRACT 风暴，§4.1.2）。故 ``on_session_end``
        的批量 EXTRACT 在此模式下不再必要（仍可显式调做去重/升华）。

        需配 ``extractor:llm``+``llm:openai`` 才真抽取；默认 ``keyword`` extractor
        走切分占位（产出 chunk 类派生单元）。未注入 extractor 时引擎降级为原文
        直接建索引 + 提交 background EXTRACT（不抛错）。
        """
        if not user_msg or not assistant_msg or self._client is None:
            return
        content = f"user: {user_msg}\nassistant: {assistant_msg}"
        logger.info(
            "[AgentMemoryMemoryProvider] sync_turn add(infer=true) scope=%s content_len=%d",
            self._scope(), len(content),
        )
        try:
            await self._client.add(
                content, self._scope(),
                tags=["conversation"], system_metadata={"infer": "true"},
            )
            logger.info("[AgentMemoryMemoryProvider] sync_turn add(infer=true) done")
        except Exception as exc:
            logger.warning("[AgentMemoryMemoryProvider] sync_turn add failed: %s", exc)

    # -- MemoryProvider: 静态提示词 + 生命周期（非抽象，有默认） -------------- #

    def system_prompt_block(self) -> str:
        return (
            "# AgentMemory Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Use agent_memory_search to find memories, agent_memory_conclude to store facts, "
            "agent_memory_procedural to store a procedural memory (goal/steps/outcome of a turn), "
            "agent_memory_profile for a full overview."
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._initialized = False

    async def on_session_end(self, messages: list[dict[str, Any]] | None = None) -> None:
        """会话结束时批量抽取一次（抑制每轮 EXTRACT 风暴，§4.1.2）。

        触发 ``evolve(EXTRACT)`` 让 AgentMemory Extractor 从本轮累积的原文抽取
        事实。需配 ``extractor:llm``+``llm:openai`` 才真抽取（默认 keyword 空转）。
        """
        if self._client is None:
            return
        logger.info("[AgentMemoryMemoryProvider] on_session_end CALLED scope=%s, triggering EXTRACT", self._scope())
        try:
            await self._client.evolve_extract(self._scope())
            logger.info("[AgentMemoryMemoryProvider] session-end EXTRACT done")
        except Exception as exc:
            logger.warning("[AgentMemoryMemoryProvider] on_session_end EXTRACT failed: %s", exc)

    # -- 内部：Scope 映射 ---------------------------------------------------- #

    def _scope(self):
        """Rail 入参 → 轻量 _Scope（不依赖 AgentMemory api 包，HTTP/进程内通用）。

        映射（§4.5）：
        - ``user_id`` → ``.user``
        - ``agent_id`` → ``.agent``
        - ``session_id`` → ``.session``（空则跨会话共享，显式传才隔离）
        - ``scope_id`` → ``.org``（作 tenant，mem0 忽略 scope_id，AgentMemory 用作租户）
        identity = target（actor==target，单租户，与 HTTP surface 一致）

        返回内置 _Scope（而非 api.Scope），让 HTTP 模式无需 AgentMemory src 在 path。
        进程内 _InProcessClient 需 api.Scope 时自行转换。
        """
        return _Scope(
            org=self._scope_id,
            user=self._user_id,
            agent=self._agent_id,
            session=self._session_id,
        )


# --------------------------------------------------------------------------- #
# AgentMemory 客户端协议（HTTP / 进程内两种实现）
# --------------------------------------------------------------------------- #


@dataclass
class _Scope:
    """轻量 Scope（org/user/agent/session 四维），不依赖 AgentMemory api 包。

    HTTP 模式直接用它的属性拼 payload；进程内模式在 _InProcessClient 里
    转成 api.Scope。这样 HTTP 模式无需 AgentMemory src 在 sys.path。
    """
    org: str = ""
    user: str = ""
    agent: str = ""
    session: str = ""


class _AgentMemoryClient:
    """AgentMemory 记忆操作的 async 客户端协议。"""

    async def add(
        self,
        content: str,
        scope,
        *,
        tags: list[str] | None = None,
        system_metadata: dict[str, str] | None = None,
        user_metadata: dict[str, str] | None = None,
    ) -> str | None:
        raise NotImplementedError

    async def search(
        self,
        query: str,
        scope,
        *,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def evolve_extract(self, scope) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _build_client(base_url: str, config_path: str | None) -> _AgentMemoryClient:
    if base_url:
        return _HttpClient(base_url)
    return _InProcessClient(config_path)


def _semantic_filter():
    """tier == semantic 的过滤子句（进程内 search 用）。"""
    from jiuwen_memory.api import FilterClause, FilterOp

    return FilterClause(field="tier", op=FilterOp.EQ, value="semantic")


# --------------------------------------------------------------------------- #
# HTTP 客户端（路径 B，推荐生产 —— 全 async，无同步方法问题）
# --------------------------------------------------------------------------- #


class _HttpClient(_AgentMemoryClient):
    """经 AgentMemory HTTP surface（``POST /v1/<verb>``）调用的 async 客户端。

    HTTP verb 响应格式（``bootstrap/core/handler.py``）：
    - ``add``    → ``{ok, op, item_id, item}``
    - ``search`` → ``{ok, op, hits:[{score, item_id, content}], count}``
    - ``list``   → ``{ok, op, items:[_unit_view], count}``
    - ``evolve`` → ``{ok, op, mode, job_id}``
    """

    def __init__(self, base_url: str) -> None:
        import httpx

        # add 触发 AgentMemory 的 background EXTRACT（含 LLM 抽取 + dedup，可能 60-90s），
        # 故 add 用长超时；search/read 用短超时。用 httpx.Timeout 分操作设置。
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
        )

    @staticmethod
    def _scope_payload(scope) -> dict[str, Any]:
        """AgentMemory HTTP surface 的 scope 映射（``handler.py:57-63``）：
        ``tenant_id`` → ``Scope.org``，``scope`` → ``Scope.user``。
        这里把四维拼成 HTTP 期望的 ``tenant_id``/``scope``/``agent_id``/``session_id``。
        """
        return {
            "tenant_id": scope.org,
            "scope": scope.user,
            "agent_id": scope.agent,
            "session_id": scope.session,
        }

    async def add(
        self, content, scope, *, tags=None, system_metadata=None, user_metadata=None
    ) -> str | None:
        payload = self._scope_payload(scope) | {
            "content": content,
            "tags": tags or [],
            "system_metadata": system_metadata or {},
            "user_metadata": user_metadata or {},
        }
        logger.info(
            "[AgentMemoryMemoryProvider] HTTP POST /v1/add tags=%s system_metadata=%s "
            "user_metadata=%s content_len=%d",
            tags, system_metadata, user_metadata, len(content),
        )
        data = await self._request("/v1/add", payload, timeout=180.0)
        logger.info(
            "[AgentMemoryMemoryProvider] /v1/add -> %s item_id=%s",
            data.get("op"), data.get("item_id"),
        )
        if not data.get("ok"):
            raise RuntimeError(f"add failed: {data.get('error')}")
        return data.get("item_id")

    async def search(
        self, query, scope, *, top_k=10
    ) -> list[dict[str, Any]]:
        # HTTP /v1/search 的 hits 不带 tier 字段（RetrievedItem 只有
        # unit_id/score/content），HTTP 模式返回全部命中（含 EPISODIC 原文）。
        payload = self._scope_payload(scope) | {"query": query, "k": top_k}
        logger.info(
            "[AgentMemoryMemoryProvider] HTTP POST /v1/search query=%r k=%d",
            query, top_k,
        )
        data = await self._request("/v1/search", payload)
        hits = data.get("hits", [])
        logger.info(
            "[AgentMemoryMemoryProvider] /v1/search -> count=%d",
            len(hits),
        )
        if not data.get("ok"):
            raise RuntimeError(f"search failed: {data.get('error')}")
        return hits

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        # HTTP list 返回全量 unit（含原文 + 派生 + 索引簿记过滤后），不按 tier 过滤。
        payload = self._scope_payload(scope)
        logger.info("[AgentMemoryMemoryProvider] HTTP POST /v1/list")
        data = await self._request("/v1/list", payload)
        items = data.get("items", [])
        logger.info(
            "[AgentMemoryMemoryProvider] /v1/list -> count=%d",
            len(items),
        )
        if not data.get("ok"):
            raise RuntimeError(f"list failed: {data.get('error')}")
        return items

    async def evolve_extract(self, scope) -> None:
        payload = self._scope_payload(scope) | {"mode": "extract"}
        logger.info("[AgentMemoryMemoryProvider] HTTP POST /v1/evolve mode=extract")
        data = await self._request("/v1/evolve", payload, timeout=180.0)
        logger.info(
            "[AgentMemoryMemoryProvider] /v1/evolve -> op=%s job_id=%s",
            data.get("op"), data.get("job_id"),
        )
        if not data.get("ok"):
            raise RuntimeError(f"evolve failed: {data.get('error')}")

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, path: str, payload: dict, *, timeout: float | None = None) -> dict:
        """统一 POST 入口：发请求 → 检查 HTTP 状态码 → 解析 JSON。

        服务器 4xx/5xx（如 502/503 返回 HTML 错误页）或返回非 JSON body 时，
        抛明确的 ``RuntimeError(HTTP <code>...)``，避免 ``r.json()`` 抛误导性的
        ``JSONDecodeError`` 被 prefetch/sync_turn/on_session_end 静默吞掉后无错误线索。
        """
        r = await self._http.post(path, json=payload, timeout=timeout) if timeout \
            else await self._http.post(path, json=payload)
        if r.is_error:
            raise RuntimeError(f"HTTP {r.status_code} from {path}: {r.text[:200]}")
        try:
            return r.json()
        except Exception as exc:
            raise RuntimeError(
                f"non-JSON response from {path} (status {r.status_code}): {r.text[:200]}"
            ) from exc


# --------------------------------------------------------------------------- #
# 进程内客户端（路径 A —— 低延迟，但 search/evolve 是同步，须 to_thread 包）
# --------------------------------------------------------------------------- #


class _InProcessClient(_AgentMemoryClient):
    """直接持有 ``LocalMemoryAPI`` 的进程内客户端。

    注意（§4.6）：``MemoryAPI`` 的 ``search``/``get``/``update``/``delete``/``evolve``
    是同步方法（内部 ``asyncio.run`` 桥接），**不能在已有事件循环里直接调**
    （会抛 ``RuntimeError: cannot be called from a running event loop``）。
    故用 ``asyncio.to_thread`` 包同步方法到线程执行。唯一可直接 ``await`` 的是
    ``add_async``。
    """

    def __init__(self, config_path: str | None) -> None:
        from jiuwen_memory.api import build_kernel
        from jiuwen_memory.config.config import Config

        config = None
        if config_path:
            import json as _json
            from pathlib import Path

            p = Path(config_path)
            data = _json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else _load_yaml(p)
            config = Config.from_dict(data)
        # 用 build_kernel 而非 assemble，同时持有 api + kv（kv 供 list_semantic 直读真源）
        kernel = build_kernel(config=config)
        self._api = kernel.api
        self._kv = kernel.kv

    @staticmethod
    def _to_api_scope(scope):
        """_Scope（轻量）→ api.Scope（进程内 AgentMemory API 期望的类型）。"""
        from jiuwen_memory.api import Scope as _ApiScope
        return _ApiScope(
            org=getattr(scope, "org", ""),
            user=getattr(scope, "user", ""),
            agent=getattr(scope, "agent", ""),
            session=getattr(scope, "session", ""),
        )

    async def add(
        self, content, scope, *, tags=None, system_metadata=None, user_metadata=None
    ) -> str | None:
        from jiuwen_memory.api import Modality
        api_scope = self._to_api_scope(scope)

        units = await self._api.add_async(
            content, api_scope,
            source=Modality.TEXT, identity=api_scope,
            tags=tags,
            system_metadata=system_metadata,
            user_metadata=user_metadata,
        )
        return units[0].id if units else None

    async def search(
        self, query, scope, *, top_k=10
    ) -> list[dict[str, Any]]:
        from jiuwen_memory.api import Context, DisclosureLevel

        api_scope = self._to_api_scope(scope)
        # 进程内模式经 search 的 tier filter 下推过滤 semantic。
        filters = [_semantic_filter()]
        result = await asyncio.to_thread(
            self._api.search,
            query,
            Context(scope=api_scope),
            identity=api_scope,
            filters=filters,
            top_k=top_k,
            disclosure=DisclosureLevel.L2,
        )
        return [
            {"content": it.content, "score": it.score, "item_id": it.unit_id}
            for it in result.items
        ]

    async def list_semantic(self, scope) -> list[dict[str, Any]]:
        # 进程内无 get_all；经 Kernel.kv 直读真源。与 HTTP /v1/list 对齐：只列建索引的
        # Memory 记忆（/memory/ 前缀），用 prefix 直取，不再全扫 + tier 过滤——/messages/
        # 原文（未建索引）与 /index/chunks/ 簿记（loads 返 None）都不在结果中。
        # （生产应给 MemoryAPI 加 get_all，见 §4.1.3。）

        api_scope = self._to_api_scope(scope)
        raw_pairs = await asyncio.to_thread(self._kv.list, api_scope, MEMORY_KEY_PREFIX)
        items = []
        for _id, raw in raw_pairs:
            unit = loads(raw)
            if unit is None:
                continue
            items.append({"content": unit.content, "item_id": unit.id, "score": 0})
        return items

    async def evolve_extract(self, scope) -> None:
        from jiuwen_memory.api import Channel, EvolveMode
        api_scope = self._to_api_scope(scope)

        # evolve 是同步+asyncio.run，必须 to_thread
        await asyncio.to_thread(
            self._api.evolve, api_scope, EvolveMode.EXTRACT, Channel.BACKGROUND, identity=api_scope
        )

    async def close(self) -> None:
        # LocalMemoryAPI 无显式 close；释放由 GC 处理
        pass


def _load_yaml(path):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML required to load YAML config; install with `uv sync --extra deploy`") from exc
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


__all__ = ["AgentMemoryMemoryProvider"]
