# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Evolver 高相似 direct_noop 短路前的实质差异检测。

score ≥ dedup_high_similarity 时，仅当候选相对已有记忆无实质差异才允许跳过 LLM
直接 NOOP；否则改走 LLM 判定（update/supersede 等）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

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
# 否定标记词——高相似下「一边肯定一边否定」是实质矛盾（喜欢 vs 不喜欢），
# 禁止 direct_noop 短路，改走 LLM 判 supersede/update。中文否定副词 + 英文 not/no
# 形式，配对称检测（仅当一边含否定一边不含才判矛盾，两边都否定不触发）。
_NEGATION_RE = re.compile(
    r"不|没|无|非|别|勿|未|否"
    r"|\b(?:not|no|never|n['’]t|without)\b",
    re.IGNORECASE,
)


def _date_spans(text: str) -> frozenset[str]:
    return frozenset(_DATE_SPAN_RE.findall(text or ""))


def _events_differ(c_event: datetime, e_event: datetime) -> bool:
    if c_event.tzinfo is None or e_event.tzinfo is None:
        if c_event.tzinfo is None and e_event.tzinfo is None:
            return c_event != e_event
        return True
    return c_event.astimezone(UTC) != e_event.astimezone(UTC)


def _temporal_conflicts(candidate: MemoryUnit, existing: MemoryUnit) -> bool:
    c_event = candidate.temporal.t_event
    e_event = existing.temporal.t_event
    if c_event is None:
        return False
    if e_event is None:
        return True
    return _events_differ(c_event, e_event)


def _has_negation_delta(candidate: MemoryUnit, existing: MemoryUnit) -> bool:
    """一边含否定标记一边不含——「喜欢 vs 不喜欢」类实质矛盾。

    高相似场景下两条内容高度重合，否定极性反转是仅有的语义分歧，直接 NOOP 会把
    矛盾修正当重复丢弃。仅当一边含否定一边不含才判矛盾（两边都肯定或都否定不触发）。
    """
    c_neg = bool(_NEGATION_RE.search(candidate.content or ""))
    e_neg = bool(_NEGATION_RE.search(existing.content or ""))
    return c_neg != e_neg


def has_meaningful_delta(candidate: MemoryUnit, existing: MemoryUnit) -> bool:
    """候选相对已有记忆是否存在禁止 direct_noop 的实质差异。"""
    if _temporal_conflicts(candidate, existing):
        return True
    if _CORRECTION_RE.search(candidate.content or ""):
        return True
    if _date_spans(candidate.content) != _date_spans(existing.content):
        return True
    if _has_negation_delta(candidate, existing):
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
