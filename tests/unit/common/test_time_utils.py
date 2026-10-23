"""``common.time_utils.first_absolute_date`` 的规则测试：共享的绝对日期提取。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.time_utils import first_absolute_date

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


def test_chinese_date_with_year() -> None:
    assert first_absolute_date("2026年6月8日我和小王去了湘菜馆", NOW) == datetime(
        2026, 6, 8, tzinfo=UTC
    )


def test_iso_date_with_dash() -> None:
    assert first_absolute_date("2026-06-08的行程", NOW) == datetime(2026, 6, 8, tzinfo=UTC)


def test_iso_date_with_slash() -> None:
    assert first_absolute_date("2026/6/8 出差", NOW) == datetime(2026, 6, 8, tzinfo=UTC)


def test_month_day_infers_year_from_now() -> None:
    assert first_absolute_date("6月8日我和小王去了湘菜馆", NOW) == datetime(2026, 6, 8, tzinfo=UTC)


def test_month_day_with_hao_suffix() -> None:
    assert first_absolute_date("6月8号团建", NOW) == datetime(2026, 6, 8, tzinfo=UTC)


def test_first_occurrence_wins() -> None:
    assert first_absolute_date("6月6日团建，6月8日吃饭", NOW) == datetime(2026, 6, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "13月40日我去了",  # 非法月日：整体取不出合法日期
        "6月83日吃饭",  # 非法日
        "2026年13月1日",  # 非法月
        "今天天气不错",  # 只有相对时间词，无绝对日期
        "just coffee",
        "",
    ],
)
def test_invalid_or_absent_date_returns_none(text: str) -> None:
    assert first_absolute_date(text, NOW) is None


def test_no_year_boundary_digits_not_swallowed() -> None:
    # 前置数字不属于日期：12026年 是非法年份，不应截出 2026 年
    assert first_absolute_date("编号12026年6月8日", NOW) is None


# -- 评审修复（PR !388 AI 审查 #1/#2/#3） --------------------------------------- #


def test_date_immediately_followed_by_time_digits() -> None:
    # 评审#1：「6月8日14点」日期后紧跟时间数字，此前被无年形态尾部 (?!\d) 排除，
    # 而带年形态经回溯能命中——两形态分叉，无年反而不出日期
    assert first_absolute_date("6月8日14点开会", NOW) == datetime(2026, 6, 8, tzinfo=UTC)
    assert first_absolute_date("我6月8日9:30出发", NOW) == datetime(2026, 6, 8, tzinfo=UTC)


def test_year_separator_insertions_keep_explicit_year() -> None:
    # 评审#3：「年」与月日之间的「的」/逗号/空白此前让带年形态漏匹配，显式年份
    # 掉进无年形态被 now.year 静默顶替为错年（不报错、返回错数据）
    assert first_absolute_date("2025年的6月8日团建", NOW) == datetime(2025, 6, 8, tzinfo=UTC)
    assert first_absolute_date("2025年，6月8日团建", NOW) == datetime(2025, 6, 8, tzinfo=UTC)
    assert first_absolute_date("2025年 6月8日团建", NOW) == datetime(2025, 6, 8, tzinfo=UTC)


def test_first_occurrence_wins_across_forms() -> None:
    # 评审#2：「第一个绝对日期」按文本位置计且跨形态一致——无年日期更靠前时，
    # 不应被更靠后的带年日期压过
    assert first_absolute_date("6月6日团建，2026年6月8日吃饭", NOW) == datetime(2026, 6, 6, tzinfo=UTC)


def test_position_order_skips_invalid_leading_with_year_form() -> None:
    # 位置优先与非法跳过的组合：非法带年日期不挡住更靠后的合法无年日期
    assert first_absolute_date("2026年13月40日，6月6日团建", NOW) == datetime(2026, 6, 6, tzinfo=UTC)
