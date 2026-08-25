"""查询时间约束解析：自然语言 → 事件时间窗 ``(time_from, time_to)``（event-time）。

**规则版**：覆盖中文常见相对时间（今天/昨天/前天/本周/上周/本月/上个月/今年/
去年/最近 N 天/过去 N 周…）。命中即返回闭/半开区间的起止 ``datetime``，未命中返回
``(None, None)``。

**属性问闸门**：属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）即使含时间词
（如「王芳今年多大了」「李华上周爱好是什么」）也不应下推事件窗——这是属性查询
而非事件时间检索，误下推会让无事件时间（``t_event=None``）的派生 unit 在索引里
被 ``t_event GTE/LT`` 按缺失字段排他。命中属性问关键词即清空 ``time_from/to``。

**LLM 钩子**：:func:`parse_time` 接受可选 ``llm``——规则未命中时可委托 LLM 解析
（如解析「去年双十一前后」这类规则难穷举的表述）。默认不启用（传 ``None``），由
调用方决定是否注入，避免规则可控性被弱模型噪声污染。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

TimeWindow = Tuple[Optional[datetime], Optional[datetime]]

# 属性问强信号关键词：命中即判为属性问，清空时间窗。
# 必须避开「会议/工作」等中性名词——误判会让真事件问句漏召回；
# 这些词是「询问对象属性」的高置信信号（多大/几岁 = 年龄、爱好 = 偏好、
# 是谁/叫什么/名字 = 身份、住址/地址 = 位置、生日 = 纪念日）。
_ATTRIBUTE_QUERY_PATTERN = re.compile(
    r"多大|几岁|多少岁|多大了|爱好|是谁|叫什么|叫什么名字|名字|"
    r"住址|住哪|住在哪|地址|生日|年龄|岁数"
)


def _day_bounds(d: datetime) -> TimeWindow:
    """执行 `day_bounds` 操作。

    Args:
        d: 参数 d（datetime）。

    Returns:
        返回 TimeWindow。
    """
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _month_start(d: datetime) -> datetime:
    """执行 `month_start` 操作。

    Args:
        d: 参数 d（datetime）。

    Returns:
        返回 datetime。
    """
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _rule_based(text: str, now: datetime) -> TimeWindow:
    """执行 `rule_based` 操作。

    Args:
        text: 参数 text（str）。
        now: 参数 now（datetime）。

    Returns:
        返回 TimeWindow。
    """
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
    """解析 ``text`` 中的事件时间约束，返回 ``(time_from, time_to)``。

    属性问（命中 ``_ATTRIBUTE_QUERY_PATTERN``）直接返回 ``(None, None)``——
    属性查询不应被误当事件时间检索下推，否则 ``t_event=None`` 的派生 unit
    会被索引按缺失字段排他，系统性空召回。
    """
    now = now or datetime.now(timezone.utc)
    if _ATTRIBUTE_QUERY_PATTERN.search(text):
        return None, None
    win = _rule_based(text, now)
    if win != (None, None):
        return win
    # 规则未命中：此处可接 LLM 解析（钩子）。默认 llm=None，不启用。
    # if llm is not None: return _llm_based(llm, text, now)
    return None, None
