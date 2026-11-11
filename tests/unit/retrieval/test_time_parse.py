"""Rule-based query time-constraint parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.retrieval.query_parser_impl.time_parse import parse_time

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


def test_yesterday_returns_day_bounds() -> None:
    lo, hi = parse_time("昨天的会议", now=NOW)

    assert lo == datetime(2026, 6, 15, tzinfo=UTC)
    assert hi == datetime(2026, 6, 16, tzinfo=UTC)


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


# -- 绝对日期：6月8日 / 2026-06-08 / 2026年6月8日 等应生成当日事件窗 ---------- #
# 缺口A（issue #252）：此前 _rule_based 只覆盖相对时间，绝对日期返回 (None, None)，
# 查询中的日期条件被整体丢弃，时间窗谓词不生成。


def test_absolute_month_day_returns_day_bounds() -> None:
    lo, hi = parse_time("我6月8日的记忆有什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)
    assert hi == datetime(2026, 6, 9, tzinfo=UTC)


def test_absolute_month_day_hao_suffix() -> None:
    lo, _ = parse_time("6月8号我和小王去了湘菜馆", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)


def test_absolute_date_with_explicit_year() -> None:
    lo, hi = parse_time("2025年6月8日的行程", now=NOW)

    assert lo == datetime(2025, 6, 8, tzinfo=UTC)
    assert hi == datetime(2025, 6, 9, tzinfo=UTC)


def test_absolute_date_iso_format() -> None:
    lo, _ = parse_time("2026-06-08发生了什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)


def test_invalid_absolute_date_returns_none() -> None:
    assert parse_time("我13月40日的记忆", now=NOW) == (None, None)


def test_attribute_query_with_absolute_date_returns_none() -> None:
    """属性问闸门对绝对日期同样生效：生日问句带日期也不下推事件窗。"""
    assert parse_time("王芳6月8日的生日是哪天", now=NOW) == (None, None)


def test_relative_time_still_wins_over_absolute_date() -> None:
    """相对时间词优先级高于绝对日期：命中相对规则即不再取日期窗口。"""
    lo, hi = parse_time("最近3天6月8日", now=NOW)

    assert lo == NOW - timedelta(days=3)
    assert hi == NOW


def test_absolute_date_followed_by_time_digits() -> None:
    """日期后紧跟时间数字（6月8日14点）仍应解析出当日窗口。"""
    lo, hi = parse_time("我6月8日14点的记忆有什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)
    assert hi == datetime(2026, 6, 9, tzinfo=UTC)


def test_year_insertion_form_resolves_explicit_year() -> None:
    """「2025年的6月8日」显式年份不得被 now.year 静默顶替。"""
    lo, hi = parse_time("2025年的6月8日的行程", now=NOW)

    assert lo == datetime(2025, 6, 8, tzinfo=UTC)
    assert hi == datetime(2025, 6, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "最近1000000000天",  # timedelta(days=10**9) 构造即溢出（上限 999999999）
        "过去1000000000周",
        "最近800000天",  # 构造成功（≈2190 年），now - delta 越过 datetime.min 溢出
        "过去200000周",  # ≈3800 年，减法溢出
    ],
)
def test_recent_n_days_overflow_returns_no_window(text: str) -> None:
    """N 超出 datetime 可表示范围：不下推时间窗（语义≈全部历史），不抛 OverflowError。"""
    assert parse_time(text, now=NOW) == (None, None)


def test_overflow_falls_through_to_later_rules() -> None:
    """相对时间溢出后继续尝试后续规则：文本同时含合法日期时取日期窗。"""
    lo, hi = parse_time("最近1000000000天，截止2026年6月8日", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)
    assert hi == datetime(2026, 6, 9, tzinfo=UTC)


@pytest.mark.parametrize("digits", [4300, 5000])
def test_huge_digit_string_returns_no_window(digits: int) -> None:
    """超长数字串触发 int(str) 4300 位上限（ValueError），与 timedelta 溢出同构回落空窗。"""
    assert parse_time("最近" + "9" * digits + "天", now=NOW) == (None, None)


@pytest.mark.parametrize("text", ["9999-12-31", "9999年12月31日"])
def test_max_date_absolute_window_returns_none(text: str) -> None:
    """绝对日期命中 datetime.max 边界（9999-12-31）：排他端越界同构回落 (None, None)。"""
    assert parse_time(text, now=NOW) == (None, None)


# -- 「年份词 + 月份」不得退化为整年 ----------------------------------------- #
@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("去年12月", datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        ("去年3月", datetime(2025, 3, 1, tzinfo=UTC), datetime(2025, 4, 1, tzinfo=UTC)),
        ("今年3月", datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        ("明年1月", datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 2, 1, tzinfo=UTC)),
        # 12 月的右端必须跨到次年 1 月 1 日，而非当年 12 月 31 日。
        ("今年12月", datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
    ],
)
def test_year_month_expression_returns_single_month_window(
    text: str,
    start: datetime,
    end: datetime,
) -> None:
    """「去年12月」这类表达给出单月窗；否则月份被年份规则吃掉，整年下推。"""
    assert parse_time(text, now=NOW) == (start, end)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("今年13月", datetime(2026, 1, 1, tzinfo=UTC), NOW),
        ("去年0月", datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_out_of_range_month_falls_back_to_year_rule(
    text: str,
    start: datetime,
    end: datetime,
) -> None:
    """月份不在 1..12 时不构造单月窗，回落到原有「今年/去年」整年语义。"""
    assert parse_time(text, now=NOW) == (start, end)
