"""查询时间约束解析：自然语言 → 事件时间窗 ``(time_from, time_to)``（event-time）。

**规则版**：覆盖中文常见相对时间（今天/昨天/前天/本周/上周/本月/上个月/今年/
去年/最近 N 天/过去 N 周…）。命中即返回闭/半开区间的起止 ``datetime``，未命中返回
``(None, None)``。

**LLM 钩子**：:func:`parse_time` 接受可选 ``llm``——规则未命中时可委托 LLM 解析
（如解析「去年双十一前后」这类规则难穷举的表述）。默认不启用（传 ``None``），由
调用方决定是否注入，避免规则可控性被弱模型噪声污染。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

TimeWindow = Tuple[Optional[datetime], Optional[datetime]]


def _day_bounds(d: datetime) -> TimeWindow:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _month_start(d: datetime) -> datetime:
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _rule_based(text: str, now: datetime) -> TimeWindow:
    t = text.strip()

    # 最近/过去 N 天｜周（到 now）
    m = re.search(r"(最近|过去|近)\s*(\d+)\s*(天|日|周|星期)", t)
    if m:
        n = int(m.group(2))
        delta = timedelta(weeks=n) if m.group(3) in ("周", "星期") else timedelta(days=n)
        return now - delta, now

    if "前天" in t:
        return _day_bounds(now - timedelta(days=2))
    if "昨天" in t or "昨日" in t:
        return _day_bounds(now - timedelta(days=1))
    if "今天" in t or "今日" in t:
        return _day_bounds(now)

    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if "上周" in t or "上星期" in t:
        return week_start - timedelta(days=7), week_start
    if "本周" in t or "这周" in t or "这星期" in t:
        return week_start, now

    month_start = _month_start(now)
    if "上个月" in t or "上月" in t:
        prev_end = month_start
        prev_start = _month_start(month_start - timedelta(days=1))
        return prev_start, prev_end
    if "本月" in t or "这个月" in t:
        return month_start, now

    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if "去年" in t:
        return year_start.replace(year=year_start.year - 1), year_start
    if "今年" in t:
        return year_start, now

    return None, None


def parse_time(
    text: str,
    now: Optional[datetime] = None,
    llm: object = None,  # LLM 钩子：注入即在规则未命中时委托其解析（默认不启用）
) -> TimeWindow:
    """解析 ``text`` 中的事件时间约束，返回 ``(time_from, time_to)``。"""
    now = now or datetime.now(timezone.utc)
    win = _rule_based(text, now)
    if win != (None, None):
        return win
    # 规则未命中：此处可接 LLM 解析（钩子）。默认 llm=None，不启用。
    # if llm is not None: return _llm_based(llm, text, now)
    return None, None
