# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""对话消息角色的构建层公共辅助（F16）。

全抽取路径（普通 / Agent / 动态 / Schema / 关键词）与中期连续性检测统一复用本模块：
- 读取角色：优先 ``system_metadata[MESSAGE_ROLE_KEY]``，只读兼容历史 ``"role"``；
- 有效消息筛选：普通抽取跳过 ``system``/``developer``，过程记忆跳过 ``system`` 保留 ``developer``；
- 角色行渲染：有角色渲染 ``[ROLE: {role}] {text}``，无角色原样返回；
- 保序键：统一按 ``(as_utc(t_ingest), unit.id)`` 消除 naive/aware 混排与同微秒抖动。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import MESSAGE_ROLE_KEY, MemoryUnit
from jiuwen_memory.common.type_def.memory import sanitize_message_role

IGNORED_EXTRACT_ROLES = frozenset({"system", "developer"})
IGNORED_PROCEDURAL_ROLES = frozenset({"system"})

_EPOCH_MIN_UTC = datetime.min.replace(tzinfo=UTC)
_CLOSING_CONTENT_TAG_RE = re.compile(r"<\s*/\s*message_content\b", re.IGNORECASE)


def message_role_of(unit: MemoryUnit) -> str:
    """读取 unit 的发言人角色；优先 ``MESSAGE_ROLE_KEY``，只读兼容旧 ``"role"``。

    纵深防御：仅接受单行可打印值（常规 ≤64 字符，含脱敏占位符总长 ≤256），
    非法历史脏值静默忽略，绝不原样插进 Prompt。
    """
    metadata = unit.system_metadata
    if not metadata:
        return ""
    for key in (MESSAGE_ROLE_KEY, "role"):
        value = sanitize_message_role(metadata.get(key))
        if value:
            return value
    return ""


def has_message_role(units: Sequence[MemoryUnit]) -> bool:
    """判断给定单元序列中是否至少有一条携带非空消息角色。"""
    return any(bool(message_role_of(unit)) for unit in units)


def is_procedural_units(units: Sequence[MemoryUnit]) -> bool:
    """判断本轮单元是否走过程记忆抽取（任一单元 ``system_metadata["procedural"]=="true"``）。"""
    return any(
        str(unit.system_metadata.get("procedural", "")).strip().lower() == "true"
        for unit in units
    )


def filter_effective_units(
    units: Sequence[MemoryUnit], *, procedural: bool = False
) -> list[MemoryUnit]:
    """筛除不应进入记忆抽取的内置指令角色消息（按 ``casefold()`` 匹配）。

    - 普通抽取（``procedural=False``）：跳过 ``system`` / ``developer``；
    - 过程记忆（``procedural=True``）：仅跳过 ``system``，保留 ``developer``；
    - 其余标准角色与所有业务自定义角色全部原样保留。
    """
    ignored = IGNORED_PROCEDURAL_ROLES if procedural else IGNORED_EXTRACT_ROLES
    return [unit for unit in units if message_role_of(unit).casefold() not in ignored]


def format_role_line(unit: MemoryUnit, content: str | None = None) -> str:
    """渲染单行带角色文本；无角色时原样返回正文，保持无 role 路径字节不变。"""
    text = unit.content if content is None else content
    role = message_role_of(unit)
    return f"[ROLE: {role}] {text}" if role else text


def fence_role_content(content: str) -> str:
    """用 ``<message_content>`` 围栏隔离不可信正文并转义闭合标签，防止正文伪造外层角色。"""
    safe_body = _CLOSING_CONTENT_TAG_RE.sub(r"<\\/message_content", content)
    return f"<message_content>\n{safe_body}\n</message_content>"


def ingest_sort_key(unit: MemoryUnit) -> tuple[datetime, str]:
    """统一按 ``(as_utc(t_ingest), unit.id)`` 排序，防 naive/aware 混排与同刻抖动。"""
    ingested = unit.temporal.t_ingest
    return (as_utc(ingested) if ingested is not None else _EPOCH_MIN_UTC, unit.id)
