# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Work-scene tools, recall and prompts, independent of provider lifecycle."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jiuwen_memory.api import install_privacy_filter, redact_for_log, scope_for_log
from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemorySceneContext, MemoryWriteRoute
from jiuwen_memory_adapter.jiuwenswarm.memory_tools import (
    CONCLUDE_SCHEMA,
    EDIT_MD_SCHEMA,
    MAX_TOP_K,
    READ_MD_SCHEMA,
    SEARCH_SCHEMA,
    VIDEO_STATUS_SCHEMA,
    VIDEO_SUBMIT_SCHEMA,
    edit_document,
    read_document,
)
from jiuwen_memory_adapter.jiuwenswarm.pipeline_profiles import DOCUMENT_FACT

logger = logging.getLogger(
    "jiuwenswarm.agents.harness.common.memory.external.agent_memory_provider"
)
install_privacy_filter(logger)

_MAX_TOP_K = MAX_TOP_K
_DEFAULT_PREFETCH_TOP_K = 5
# prefetch 直接注入用户画像：USER.md 不分项目、条目少、与多数任务相关，
# 不经相关度筛选——overlap 重排按共同词打分，换一种问法画像条目就可能得 0 分被丢。
# 通用预取先过滤明文凭据与预签名链接；超出上限时保留末尾（最新）的条目。
_PROFILE_MD_FILENAME = "USER.md"
_PROFILE_INJECT_MAX_ITEMS = 20
_PROFILE_INJECT_MAX_CHARS = 2000
_DEFAULT_SEARCH_TOP_K = 10

_RECALL_TIERS = ("semantic", "core", "procedural")


def _recall_filters(project: str = "") -> dict[str, Any]:
    """prefetch 召回过滤（dict DSL，HTTP/进程内两模式共用）。

    实测确认（design doc §8.2.1）：``{"tier": {"in": [...]}}`` 被服务端真实执行，
    episodic 原文被正确排除。进程内路径经 ``RetrievalQuery`` 边界的 ``normalize``
    接收同一份 dict DSL。

    ``project`` 非空时追加 ``system_metadata.project = project``（顶层多键
    默认 AND），与 ``agent_memory_search`` 同口径：仅召回本项目，画像单独读取 USER.md。
    不带该谓词时影子索引兜底 ``project = ''``，项目内写入的记忆在同项目会话的
    prefetch 里取不到（F08 D2）。
    """
    filters: dict[str, Any] = {
        "tier": {"in": list(_RECALL_TIERS)},
        "system_metadata.memory_class": {"ne": "team_memory"},
        "system_metadata.is_credential": {"ne": "true"},
    }
    if project:
        filters["system_metadata.project"] = {"eq": project}
    return filters


def _tail_within_budget(
    items: list[dict[str, Any]], *, max_items: int, max_chars: int
) -> list[dict[str, Any]]:
    """从列表末尾起取条数与字数预算内的条目（放不下的跳过），返回时保持原顺序。"""
    kept: list[dict[str, Any]] = []
    used = 0
    for item in reversed(items):
        size = len(item.get("content", ""))
        if len(kept) >= max_items:
            break
        if used + size > max_chars:
            # 放不下的条目跳过、继续向前取：单条超长不应让整个画像空掉
            continue
        kept.append(item)
        used += size
    kept.reverse()
    return kept


def _merge_prefetch_lines(
    profile: list[dict[str, Any]], items: list[dict[str, Any]]
) -> list[str]:
    """用户画像条目在前、检索命中在后，按 item_id 与正文去重，返回正文列表。"""
    lines: list[str] = []
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    for item in [*profile, *items]:
        if item.get("is_credential"):
            continue
        content = item.get("content", "")
        item_id = item.get("item_id") or ""
        # 空 item_id 从不进 seen_ids，无需单独判空
        if not content or content in seen_text or item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)
        seen_text.add(content)
        lines.append(content)
    return lines


class WorkMemoryScene:
    """Preserve Work defaults while allowing independent scene instances."""

    @staticmethod
    def write_route(operation: str = "fact") -> MemoryWriteRoute:
        """Ordinary tools and automatic turns share the same write profile."""
        if operation != "fact":
            raise ValueError("Unsupported document write operation")
        return MemoryWriteRoute(DOCUMENT_FACT, True)

    @staticmethod
    def sdk_config_overrides() -> dict[str, Any]:
        """Work preserves the selected SDK baseline."""
        return {}

    @staticmethod
    def resolve_project(options: Mapping[str, Any]) -> str:
        """Resolve the per-call Work project without binding a Coding project."""
        return str(options.get("project") or "").strip()

    @staticmethod
    def get_tool_schemas(context: MemorySceneContext) -> list[dict[str, Any]]:
        """返回 OpenAI 风格 schema；Rail 自动包成 ToolCard（§4.8）。

        [本地修改 2026-10-17] ``agent_memory_profile`` / ``agent_memory_procedural``
        **不在注册列表**——LLM 看不到、不会主动调用；但两者的 schema 与
        ``handle_tool_call`` 实现完整保留，供内部直调（测试 / 排查 / 后续
        重新注册时直接加回列表即可）。
        """
        schemas = [SEARCH_SCHEMA, CONCLUDE_SCHEMA, READ_MD_SCHEMA, EDIT_MD_SCHEMA]
        if context.video_enabled():
            schemas.extend((VIDEO_SUBMIT_SCHEMA, VIDEO_STATUS_SCHEMA))
        if context.continuity_enabled:
            from jiuwen_memory_adapter.continuity import tool_schemas

            # 工具清单跟部署档位走：接续档不注册关系工具，治理工具永不进 Agent 清单。
            schemas.extend(tool_schemas(mode=context.continuity_mode))
        logger.info(
            "[AgentMemoryMemoryProvider] get_tool_schemas CALLED -> returning %d schemas: %s",
            len(schemas),
            [s.get("name") for s in schemas],
        )
        return deepcopy(schemas)

    async def handle_tool_call(
        self, context: MemorySceneContext, tool_name: str, args: dict[str, Any],
    ) -> str:
        """Dispatch the Work tool set without selecting another memory scene."""
        handlers = {
            "agent_memory_profile": self._profile,
            "agent_memory_search": self._search,
            "agent_memory_conclude": self._conclude,
            "agent_memory_procedural": self._procedural,
            "agent_memory_read_md_file": read_document,
            "agent_memory_edit_md_file": edit_document,
            "agent_memory_video_submit": self._video_submit,
            "agent_memory_video_status": self._video_status,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            logger.info("[AgentMemoryMemoryProvider] handle_tool_call unknown tool=%s", tool_name)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        if tool_name in {"agent_memory_video_submit", "agent_memory_video_status"}:
            if not context.video_enabled():
                return json.dumps({"error": "Video memory tools are disabled"})
        return await handler(context, args)

    @staticmethod
    async def _video_submit(context: MemorySceneContext, args: dict[str, Any]) -> str:
        """Validate a local video URI before submitting the existing ingest job."""
        uri = args.get("uri")
        if not isinstance(uri, str) or not uri.startswith("file:///"):
            return json.dumps({"error": "A local file:/// video URI is required"})
        project = str(args.get("project") or "").strip()
        result = await context.client.submit_video(uri, context.scope, project=project)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    async def _video_status(context: MemorySceneContext, args: dict[str, Any]) -> str:
        """Look up the host-authorized video job."""
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return json.dumps({"error": "An ingest job ID is required"})
        result = await context.client.video_status(job_id.strip(), context.scope)
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    async def _profile(context: MemorySceneContext, _args: dict[str, Any]) -> str:
        """Handle the existing Work profile tool."""
        scope = context.scope
        items = await context.client.list_semantic(scope)
        if not items:
            logger.info("[AgentMemoryMemoryProvider] agent_memory_profile -> no memories")
            return json.dumps({"result": "No memories stored yet."})
        lines = [it["content"] for it in items if it.get("content")]
        logger.info(
            "[AgentMemoryMemoryProvider] agent_memory_profile -> count=%d", len(lines)
        )
        for idx, line in enumerate(lines):
            logger.info(
                "[AgentMemoryMemoryProvider] profile hit[%d]: %s",
                idx,
                redact_for_log(line),
            )
        return json.dumps({"result": "\n".join(lines), "count": len(lines)})

    @staticmethod
    async def _search(context: MemorySceneContext, args: dict[str, Any]) -> str:
        """Handle the existing Work search tool."""
        scope = context.scope
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "Missing required parameter: query"})
        # [本地修改 2026-10-17] LLM 可能显式传 "top_k": null——key 存在时
        # dict.get 的缺省值不生效，int(None) 直接 TypeError。用 or 同时覆盖
        # 「key 缺省」与「显式 null」（top_k=0 无意义，同样回落缺省）。
        top_k = min(int(args.get("top_k") or _DEFAULT_SEARCH_TOP_K), _MAX_TOP_K)
        # 项目过滤下推到检索端；跨项目画像通过 USER.md 单独读取。
        project = str(args.get("project") or "").strip()
        filters = {"system_metadata.memory_class": {"ne": "team_memory"}}
        if project:
            filters["system_metadata.project"] = {"eq": project}
        # 显式检索不过滤 tier（§6.2-F）：LLM 主动检索允许命中 EPISODIC 原文。
        items = await context.client.search(query, scope, top_k=top_k, filters=filters)
        if not items:
            logger.info(
                "[AgentMemoryMemoryProvider] agent_memory_search query=%s -> no relevant",
                redact_for_log(query),
            )
            return json.dumps({"result": "No relevant memories found."})
        payload = [
            {"memory": it.get("content", ""), "score": it.get("score", 0)} for it in items
        ]
        logger.info(
            "[AgentMemoryMemoryProvider] agent_memory_search query=%s -> count=%d",
            redact_for_log(query),
            len(payload),
        )
        for idx, p in enumerate(payload):
            logger.info(
                "[AgentMemoryMemoryProvider] search hit[%d] score=%s: %s",
                idx,
                p.get("score"),
                redact_for_log(p.get("memory")),
            )
        return json.dumps({"results": payload, "count": len(payload)})

    async def _conclude(self, context: MemorySceneContext, args: dict[str, Any]) -> str:
        """Handle the existing Work conclude tool."""
        scope = context.scope
        conclusion = args.get("conclusion", "")
        if not conclusion:
            return json.dumps({"error": "Missing required parameter: conclusion"})
        # infer=true：conclude 走 LLM 抽取（与 sync_turn 同口径），抽取派生的
        # 记忆按 memory_class + project 分流落盘。project 作标量
        # system_metadata["project"] 直传（agent 场景无 router，coords 嵌套
        # dict 会被 _reject_non_scalar_metadata 拒）。
        tags = args.get("tags") or ["conclude"]
        project = str(args.get("project") or "").strip()
        system_metadata: dict[str, str] = {
            **self.write_route().metadata(),
            "source_app": "jiuwenswarm",
            "agent_label": context.scope.agent,
        }
        if project:
            system_metadata["project"] = project
            # 显式指定项目时防止凭据在 project_fact=false 下逃逸到全局 MEMORY.md；
            # 普通 user_profile + project_fact=false 仍按上游契约写入全局 USER.md。
            system_metadata["project_scope"] = "fixed"
        data = await context.client.add(
            conclusion,
            scope,
            tags=tags,
            system_metadata=system_metadata,
        )
        if data is None:
            return json.dumps({"error": "AgentMemory add failed (server unavailable)."})
        logger.info(
            "[AgentMemoryMemoryProvider] agent_memory_conclude stored=%s "
            "tags=%s project=%s",
            redact_for_log(conclusion),
            tags,
            project or "",
        )
        if data.item_id is None:
            return json.dumps({"result": "Fact stored (deduped, no new unit)."})
        return json.dumps({"result": "Fact stored.", "item_id": data.item_id})

    @staticmethod
    async def _procedural(context: MemorySceneContext, args: dict[str, Any]) -> str:
        """Handle the existing Work procedural tool."""
        scope = context.scope
        content = args.get("content", "")
        if not content:
            return json.dumps({"error": "Missing required parameter: content"})
        # 过程记忆：经 add(procedural=true) 触发 engine 的 procedural 分支——
        # 原文不落 KV，evolver 让 extractor 把本轮汇总成 1 条 PROCEDURAL 执行历史，
        # 落 /memory/ 建索引；不走去重、不收集 context（见 F02「过程记忆抽取」）。
        # 需配 extractor:llm 才真汇总（默认 keyword 降级为原文原样存 1 条 PROCEDURAL）。
        data = await context.client.add(
            content,
            scope,
            system_metadata={
                "procedural": "true",
                "source_app": "jiuwenswarm",
                "agent_label": context.scope.agent,
            },
        )
        if data is None:
            return json.dumps({"error": "AgentMemory add failed (server unavailable)."})
        logger.info(
            "[AgentMemoryMemoryProvider] agent_memory_procedural summarized=%s item_id=%s",
            redact_for_log(content),
            data.item_id,
        )
        # add 返回 item_id=None：extractor 产空（LLM 返回不可解析/未产出候选）→
        # 未持久化任何记忆。不可报成功（false success）——evolver 吞掉 LLM 失败
        # 返回空 EvolveResult，需如实告知调用方。content 已在上文校验非空。
        if not data.item_id:
            return json.dumps(
                {
                    "error": "Procedural memory not stored: extractor produced nothing "
                    "(LLM returned unparseable content or no candidates).",
                }
            )
        return json.dumps({"result": "Procedural memory stored.", "item_id": data.item_id})

    async def prefetch(self, context: MemorySceneContext, query: str, **kwargs: Any) -> str:
        """Recall distilled Work memories and inject the bounded user profile."""
        if not query:
            return ""
        # §6.2-E：剥 TUI 信封，用纯 content 检索，避免命中对话原文壳。
        search_query = context.prepare(query)
        if search_query != query:
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch stripped envelope: %s -> %s",
                redact_for_log(query),
                redact_for_log(search_query),
            )
        # [本地修改 2026-10-17] 同 agent_memory_search：null 防御（or 覆盖缺省与显式 null）。
        top_k = min(int(kwargs.get("top_k") or _DEFAULT_PREFETCH_TOP_K), _MAX_TOP_K)
        # rail 经 _resolve_project 传入 project；与 sync_turn / agent_memory_search 同口径。
        project = str(kwargs.get("project") or "").strip()
        try:
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch before search scope=%s top_k=%d",
                scope_for_log(context.scope),
                top_k,
            )
            # §6.2-F：prefetch 只召回提炼产物（semantic/core/procedural）。
            try:
                items = await context.client.search(
                    search_query,
                    context.scope,
                    top_k=top_k,
                    filters=_recall_filters(project),
                )
            except Exception as exc:
                # 检索失败不连带丢掉用户画像：降级为只注入 USER.md 的条目
                logger.warning(
                    "[AgentMemoryMemoryProvider] prefetch search failed, profile only: "
                    "error_type=%s",
                    type(exc).__name__,
                    exc_info=True,
                )
                items = []
            logger.info("[AgentMemoryMemoryProvider] prefetch after search items=%d", len(items))
            profile = await self.profile_items(context)
            lines = _merge_prefetch_lines(profile, items)
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch search_query=%s scope=%s "
                "top_k=%d recalled=%d profile=%d injected=%d",
                redact_for_log(search_query),
                scope_for_log(context.scope),
                top_k,
                len(items),
                len(profile),
                len(lines),
            )
            for idx, line in enumerate(lines):
                logger.info(
                    "[AgentMemoryMemoryProvider] prefetch hit[%d]: %s",
                    idx,
                    redact_for_log(line),
                )
            return (
                ("## AgentMemory Memory\n" + "\n".join(f"- {line}" for line in lines))
                if lines
                else ""
            )
        except Exception as exc:
            logger.warning(
                "[AgentMemoryMemoryProvider] prefetch failed: error_type=%s",
                type(exc).__name__,
                exc_info=True,
            )
            return ""

    @staticmethod
    async def profile_items(context: MemorySceneContext) -> list[dict[str, Any]]:
        """读 USER.md 的条目供 prefetch 直接注入；任何失败都降级为空，不影响检索结果。

        文件不存在、非文档模式、传输失败都返回空列表。通用预取先过滤明文凭据与预签名
        链接条目；超出条数或字数上限时保留文件末尾的最新条目。
        """
        try:
            items = await context.client.read_md_file(_PROFILE_MD_FILENAME, context.scope)
        except Exception as exc:
            logger.info(
                "[AgentMemoryMemoryProvider] prefetch profile unavailable: error_type=%s",
                type(exc).__name__,
            )
            return []
        safe_items = [
            it
            for it in items or []
            if it.get("content") and not it.get("is_credential")
        ]
        return _tail_within_budget(
            safe_items,
            max_items=_PROFILE_INJECT_MAX_ITEMS,
            max_chars=_PROFILE_INJECT_MAX_CHARS,
        )

    @staticmethod
    def sync_metadata(_context: MemorySceneContext) -> dict[str, str]:
        return {}

    @staticmethod
    def system_prompt(context: MemorySceneContext) -> str:
        """Describe Work tools and enabled capabilities to the model."""
        # [本地修改 2026-10-17] 显式路由规则：新增→conclude（append-only），
        # 修改/更正→read_md_file 定位 + edit_md_file 原地替换。个人信息可能在
        # USER.md 也可能在 daily 文件（YYYY-MM-DD.md），修改前先读定位。
        prompt = (
            "# AgentMemory Memory\n"
            f"Active. User: {context.scope.user}.\n"
            "Tool routing: agent_memory_search finds memories by meaning. "
            "agent_memory_conclude ONLY appends a brand-new fact — it never modifies "
            "existing memories. To CHANGE or CORRECT a stored memory, first locate it "
            "with agent_memory_read_md_file (USER.md / MEMORY.md / daily YYYY-MM-DD.md — "
            "personal info may be in a daily file), then agent_memory_edit_md_file to "
            "replace it in place.\n"
            "Reusable identifiers: When a task needs an email address, account, phone number, "
            "server/endpoint address, or service credential missing from the current message "
            "and injected memory, call agent_memory_search first using the task purpose, role, "
            "or service/account name (omit project to search cross-project identifiers or "
            "credentials) before asking the user to re-enter it. Use a unique match "
            "directly when it fits the task; when multiple reasonable candidates exist or the "
            "stored purpose does not match, ask the user to confirm before acting. Credentials "
            "(passwords, authorization codes, keys, tokens) are excluded from general prefetch "
            "and should be looked up only when needed for authentication; when asking the user "
            "to choose among credentials, show only the service or account name, never raw "
            "secrets."
        )
        if context.video_enabled():
            prompt += (
                "\nWhen the user asks to remember a video by local file URI, "
                "use agent_memory_video_submit with that URI. "
                "Use agent_memory_video_status to check the resulting job."
            )
        if context.continuity_enabled:
            prompt += (
                " Restore saved task background with agent_memory_search (task_id) before "
                "continuing; select a duty to expand typed relations and inspect paths/gaps. "
                "Record observations/results through agent_memory_add with the continuity "
                "parameter, and review relations explicitly. "
                "No predecessor handoff is required; Rail hooks do not capture all history."
            )
        return prompt
