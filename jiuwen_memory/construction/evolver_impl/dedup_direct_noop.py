# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Evolver 高相似 direct_noop 短路前的实质差异检测。

score ≥ dedup_high_similarity 时，仅当候选相对已有记忆无实质差异才允许跳过 LLM
直接 NOOP；否则改走 LLM 判定（update/supersede 等）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from jiuwen_memory.common.type_def import MemoryUnit

_CORRECTION_RE = re.compile(
    r"改为|改成|变更为|更新为|调整为|推迟|提前|延后|取消|不再"
    r"|\b(?:changed to|updated to|adjusted to|postponed|rescheduled|delayed"
    r"|brought forward|cancelled|canceled|no longer)\b",
    re.IGNORECASE,
)
_DATE_SPAN_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月|\d{1,2}月"
)


def _date_spans(text: str) -> frozenset[str]:
    return frozenset(_DATE_SPAN_RE.findall(text or ""))


def _events_differ(c_event: datetime, e_event: datetime) -> bool:
    if c_event.tzinfo is None or e_event.tzinfo is None:
        if c_event.tzinfo is None and e_event.tzinfo is None:
            return c_event != e_event
        return True
    return c_event.astimezone(timezone.utc) != e_event.astimezone(timezone.utc)


def _temporal_conflicts(candidate: MemoryUnit, existing: MemoryUnit) -> bool:
    c_event = candidate.temporal.t_event
    e_event = existing.temporal.t_event
    if c_event is None:
        return False
    if e_event is None:
        return True
    return _events_differ(c_event, e_event)


def has_meaningful_delta(candidate: MemoryUnit, existing: MemoryUnit) -> bool:
    """候选相对已有记忆是否存在禁止 direct_noop 的实质差异。"""
    if _temporal_conflicts(candidate, existing):
        return True
    if _CORRECTION_RE.search(candidate.content or ""):
        return True
    if _date_spans(candidate.content) != _date_spans(existing.content):
        return True
    return False


def should_direct_noop(
    score: float,
    high_threshold: float,
    candidate: MemoryUnit,
    existing: MemoryUnit,
) -> bool:
    """高相似且无语义/时效实质差异时，才允许跳过 LLM 直接 NOOP。"""
    return (
        score >= high_threshold
        and not has_meaningful_delta(candidate, existing)
    )
