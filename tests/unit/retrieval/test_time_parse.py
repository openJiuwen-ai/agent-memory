"""Rule-based query time-constraint parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.retrieval.query_parser_impl.time_parse import parse_time

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


def test_yesterday_returns_day_bounds() -> None:
    lo, hi = parse_time("昨天的会议", now=NOW)

    assert lo == datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert hi == datetime(2026, 6, 16, tzinfo=timezone.utc)


def test_recent_n_days_window_ends_now() -> None:
    lo, hi = parse_time("最近3天", now=NOW)

    assert lo == NOW - timedelta(days=3)
    assert hi == NOW


def test_no_time_expression_returns_none() -> None:
    assert parse_time("just coffee", now=NOW) == (None, None)


# -- 属性问闸门：含时间词但意图是属性查询时，清空 time_from/to 不下推事件窗 ---- #
# 属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）含「今年/昨天」时若按事件时间
# 窗下推，会把 t_event=None 的派生 unit 按缺失字段排他，系统性空召回。
@pytest.mark.parametrize(
    "query",
    [
        "王芳今年多大了",
        "李华今年几岁了",
        "陈静的爱好是什么",
        "王芳是谁",
        "李华的家庭住址在哪里",
        "陈静的生日是哪天",
        "王芳的年龄多少岁",
        "昨天聊了王芳的爱好",  # 含时间词但中心是属性问
    ],
)
def test_attribute_query_with_time_word_returns_none(query: str) -> None:
    """属性问命中关键词即清空时间窗，即便 query 含「今年/昨天」等时间词。"""
    assert parse_time(query, now=NOW) == (None, None)


@pytest.mark.parametrize(
    "query",
    [
        "昨天的会议",  # "会议"不是属性问关键词
        "上周做了什么",
        "最近3天发生了什么",
        "今天的工作记录",
    ],
)
def test_event_query_still_emits_window(query: str) -> None:
    """非属性问的事件问句仍正常解析时间窗。"""
    lo, hi = parse_time(query, now=NOW)
    assert lo is not None and hi is not None, f"事件问句 {query!r} 应有时间窗"
