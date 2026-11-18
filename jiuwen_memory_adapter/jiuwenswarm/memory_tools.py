# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared document tool schemas and read/edit operations."""
from __future__ import annotations

import json
import logging
from typing import Any

from jiuwen_memory.api import install_privacy_filter, redact_for_log
from jiuwen_memory_adapter.jiuwenswarm.memory_scene import MemorySceneContext

logger = logging.getLogger(
    "jiuwenswarm.agents.harness.common.memory.external.agent_memory_provider"
)
install_privacy_filter(logger)
_UNBOUNDED_END = 10**9
MAX_TOP_K = 50

VIDEO_SUBMIT_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_video_submit",
    "description": (
        "Submit a local video file URI to the memory engine for analysis and storage. "
        "Returns a job ID. Use agent_memory_video_status to check completion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "Local file:/// video URI"},
            "project": {
                "type": "string",
                "description": "Project coordinate. Omit to use the current project, if set.",
            },
        },
        "required": ["uri"],
    },
}

VIDEO_STATUS_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_video_status",
    "description": "Check a submitted video memory job and return its resulting memory IDs.",
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
}

PROFILE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_profile",
    "description": (
        "Retrieve stored memories about the user — preferences, facts, project context. "
        "Use at conversation start for a full overview."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

READ_MD_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_read_md_file",
    "description": (
        "Read a markdown memory file by filename and return the memory blocks it "
        "contains. md_filename is a basename: USER.md (user profile), MEMORY.md "
        "(long-term memory), or a daily file like 2026-09-30.md. Optionally read "
        "only a line range via offset/limit (1-based lines)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "md_filename": {
                "type": "string",
                "description": "Basename of the md file, e.g. USER.md, MEMORY.md, 2026-09-30.md.",
            },
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to read the cross-project file (memory root)."
                ),
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line (default: 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read (default: whole file).",
            },
        },
        "required": ["md_filename"],
    },
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
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to search only cross-project memories "
                    "(those stored without a project). When set, recalls this "
                    "project's memories plus cross-project memories."
                ),
            },
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_conclude",
    "description": (
        # [本地修改 2026-10-17] 原描述含 "corrections"，会把「修改个人信息」类指令
        # 路由到 conclude（append 式写入）而非 edit_md_file（原地替换），产生新旧
        # 矛盾事实共存。改为明确 append-only + 把更正路由到 edit_md_file。
        # [本地修改] infer 恢复为 true：conclude 走 LLM 抽取并分类落盘，但语义仍是
        # 新增不改已有（更正仍路由到 edit_md_file）。
        "Store a new durable memory (extracted and classified). It NEVER modifies or "
        "removes existing memories — to change or correct what is already stored, use "
        "agent_memory_edit_md_file (read the file first to locate the exact text)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact/memory to store."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional labels for later filtering.",
            },
            "project": {
                "type": "string",
                "description": "Project coordinate; omit for cross-project storage.",
            },
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
                "description": (
                    "The conversation/turn content to summarize into a procedural memory."
                ),
            }
        },
        "required": ["content"],
    },
}

EDIT_MD_SCHEMA: dict[str, Any] = {
    "name": "agent_memory_edit_md_file",
    "description": (
        "Modify or correct an EXISTING memory — the only way to change what is already "
        "stored: replaces old_text with new_text in place (old_text must appear exactly "
        "once in the file, an error otherwise; the edit supersedes the affected memory "
        "units and returns the new ones). Use agent_memory_read_md_file FIRST to locate "
        "the exact current text — personal info may live in USER.md or a daily file "
        "(YYYY-MM-DD.md)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "md_filename": {
                "type": "string",
                "description": "Basename of the md file, e.g. USER.md, MEMORY.md, 2026-09-30.md.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to replace; must be unique within the file.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text (empty string deletes old_text).",
            },
            "project": {
                "type": "string",
                "description": (
                    "Project coordinate; omit to edit the cross-project file (memory root)."
                ),
            },
        },
        "required": ["md_filename", "old_text", "new_text"],
    },
}


def parse_line_range(args: dict) -> tuple[int, int] | None:
    """read 工具 ``offset``/``limit`` → 内核 ``line_range``（1-based 闭区间，md文件接口 F08）。

    offset 缺省 1；limit 缺省 → 无上界（end 传 ``_UNBOUNDED_END``，内核 clamp
    到文件实际行数）；两者都缺省 → None（整文件）。``limit <= 0`` 得
    ``end < start``，内核切出空串（读 0 行），如实返回空。
    """
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is None and limit is None:
        return None
    start = max(int(offset or 1), 1)
    if limit is None:
        return (start, _UNBOUNDED_END)
    return (start, start + int(limit) - 1)


async def read_document(context: MemorySceneContext, args: dict[str, Any]) -> str:
    """Read complete document blocks with the existing line-range semantics."""
    scope = context.scope
    md_filename = str(args.get("md_filename") or "")
    if not md_filename:
        return json.dumps({"error": "Missing required parameter: md_filename"})
    project = str(args.get("project") or "")
    line_range = parse_line_range(args)
    # md文件接口 F08：按 md_filename + 行范围读 md 文件，返回命中的 MemoryUnit 块。
    # 文件不存在/行范围无完整块 → 空列表（内核 read_content 容错空串）；
    # 非文档模式服务端抛 ValidationError（经 client 透出）。
    units = await context.client.read_md_file(
        md_filename, scope, project=project, line_range=line_range
    )
    if units is None:
        return json.dumps(
            {"error": "AgentMemory read_md_file failed (server unavailable)."}
        )
    if not units:
        logger.info(
            "[AgentMemoryMemoryProvider] agent_memory_read_md_file file=%s "
            "-> no blocks",
            md_filename,
        )
        return json.dumps({"result": f"No memory blocks found in {md_filename}."})
    blocks = [
        {
            "item_id": it.get("item_id", ""),
            "tier": it.get("tier", ""),
            "memory": it.get("content", ""),
        }
        for it in units
    ]
    logger.info(
        "[AgentMemoryMemoryProvider] agent_memory_read_md_file file=%s -> count=%d",
        md_filename,
        len(blocks),
    )
    for idx, blk in enumerate(blocks):
        logger.info(
            "[AgentMemoryMemoryProvider] read_md hit[%d]: %s",
            idx,
            redact_for_log(blk.get("memory")),
        )
    return json.dumps(
        {"md_filename": md_filename, "count": len(blocks), "blocks": blocks}
    )


async def edit_document(context: MemorySceneContext, args: dict[str, Any]) -> str:
    """Apply a unique text replacement and return the resulting memory units."""
    scope = context.scope
    md_filename = str(args.get("md_filename") or "")
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    if not md_filename:
        return json.dumps({"error": "Missing required parameter: md_filename"})
    if not old_text:
        return json.dumps({"error": "Missing required parameter: old_text"})
    # new_text 允许空串（old_text → "" 即删除该片段）。
    project = str(args.get("project") or "")
    # md文件接口 F08：按 md_filename + old_text 文件内唯一替换，继承式 SUPERSEDE——
    # 返回受影响的**新** unit（旧版已失效）。old_text 不存在/不唯一由
    # 内核抛 NotFoundError/ValidationError（经 client 透出给 LLM）。
    units = await context.client.edit_md_file(
        md_filename, scope, old_text, new_text, project=project
    )
    if units is None:
        return json.dumps(
            {"error": "AgentMemory edit_md_file failed (server unavailable)."}
        )
    blocks = [
        {
            "item_id": it.get("item_id", ""),
            "tier": it.get("tier", ""),
            "memory": it.get("content", ""),
        }
        for it in units
    ]
    logger.info(
        "[AgentMemoryMemoryProvider] agent_memory_edit_md_file file=%s -> count=%d",
        md_filename,
        len(blocks),
    )
    return json.dumps(
        {
            "result": "Edited.",
            "md_filename": md_filename,
            "count": len(blocks),
            "units": blocks,
        }
    )
