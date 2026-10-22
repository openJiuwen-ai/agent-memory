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
