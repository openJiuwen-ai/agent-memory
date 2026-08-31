"""direct_noop 实质差异检测单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment, Temporal
from jiuwen_memory.construction.evolver_impl.dedup_direct_noop import (
    has_meaningful_delta,
    should_direct_noop,
)

pytestmark = pytest.mark.unit

_MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)
_MAY = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _unit(content: str, *, t_event: datetime | None = None) -> MemoryUnit:
    return MemoryUnit(
        id="u1",
        scope=Scope(org="test", user="alice", agent="a1", session="s1"),
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(t_event=t_event),
    )


def test_same_content_no_delta():
    existing = _unit("用户偏好 Python")
    candidate = _unit("用户偏好 Python")
    assert has_meaningful_delta(candidate, existing) is False
    assert should_direct_noop(1.0, 0.9, candidate, existing) is True


def test_month_in_content_is_delta():
    existing = _unit("会议定于3月举行")
    candidate = _unit("会议定于5月举行")
    assert has_meaningful_delta(candidate, existing) is True
    assert should_direct_noop(0.95, 0.9, candidate, existing) is False


def test_correction_word_is_delta():
    existing = _unit("会议定于3月举行")
    candidate = _unit("会议改为5月举行")
    assert has_meaningful_delta(candidate, existing) is True


def test_english_correction_word_is_delta():
    existing = _unit("Meeting scheduled for March")
    candidate = _unit("Meeting changed to May")
    assert has_meaningful_delta(candidate, existing) is True
    assert should_direct_noop(0.95, 0.9, candidate, existing) is False


def test_t_event_conflict_is_delta():
    """t_event 不同（含正文月份差异）→ 禁止 direct_noop。"""
    existing = _unit("公司年度大会举办时间已确认2026年3月", t_event=_MARCH)
    candidate = _unit("公司年度大会举办时间已确认2026年5月", t_event=_MAY)
    assert has_meaningful_delta(candidate, existing) is True


def test_t_event_naive_aware_mismatch_is_delta():
    """naive 与 aware 混比不抛错，视为有时效差异。"""
    existing = _unit("公司年度大会举办时间已确认", t_event=datetime(2026, 3, 1))
    candidate = _unit("公司年度大会举办时间已确认", t_event=_MAY)
    assert has_meaningful_delta(candidate, existing) is True


def test_t_event_same_instant_different_tz_no_delta():
    """同一时刻不同 tz 表示 → 无时效冲突。"""
    existing = _unit(
        "公司年度大会举办时间已确认",
        t_event=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
    )
    candidate = _unit(
        "公司年度大会举办时间已确认",
        t_event=datetime(2026, 3, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert has_meaningful_delta(candidate, existing) is False


def test_score_below_high_never_direct_noop():
    """近义改写、无实质差异，但 score 略低于 high：仍不能 direct_noop。"""
    existing = _unit("用户偏好简洁回答风格")
    candidate = _unit("用户喜欢简洁明了的回答方式")
    assert has_meaningful_delta(candidate, existing) is False
    assert should_direct_noop(0.88, 0.9, candidate, existing) is False
