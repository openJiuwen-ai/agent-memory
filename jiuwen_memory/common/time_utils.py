# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆时间的公共解释规则：不依赖进程本地时区，不改写输入值。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# 绝对日期共享规则：查询侧解析时间窗与写入侧抽取事件时间用同一份正则，
# 保证同一日期字符串在两侧解析到同一天——这是时间窗能命中的前提。
# 两种形态：带年（2026年6月8日 / 2026-06-08 / 2026/6/8）；无年（6月8日/6月8号）。
# 无年形态前置断言排除数字与「年」：既不吃掉相邻数字，也不把「2026年13月1日」
# 这类非法带年日期截成「13月1日」重新解释——非法年份整体作废，不降级匹配。
_ABSOLUTE_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:[日号])?(?!\d)"),
    re.compile(r"(?<![\d年])(\d{1,2})月(\d{1,2})[日号](?!\d)"),
)


def as_utc(value: datetime) -> datetime:
    """无时区时间按 UTC 解释；有时区时间保留实际时刻，比较保留微秒。"""
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value: datetime) -> int:
    """按公共时间规则投影为 epoch 毫秒；缺值与哨兵由调用方处理。"""
    return int(as_utc(value).timestamp() * 1000)


def first_absolute_date(text: str, now: datetime) -> datetime | None:
    """提取文本中第一个绝对日期，返回当日零点（时区取 ``now`` 的时区）。

    - 带年份的日期用显式年份；无年份按 ``now.year`` 补齐。查询侧与写入侧共用
      本规则，同一日期字符串在两侧解析到同一天；跨年指代（如 1 月说「去年6月」）
      属相对时间词，由查询侧相对规则负责，不在本函数处理。
    - 非法日期（6月32日、13月）跳过并尝试后续匹配；全部不可解析返回 ``None``。
    - 区间表述（「6月20日到21日」）取首日；跨度建模归时间树/区间结构，此处不展开。
    """
    tzinfo = now.tzinfo or UTC
    for pattern in _ABSOLUTE_DATE_PATTERNS:
        for match in pattern.finditer(text):
            # 带年形态分组为 (year, month, day)，无年形态为 (month, day)。
            *year, month, day = match.groups()
            try:
                return datetime(
                    int(year[0]) if year else now.year,
                    int(month),
                    int(day),
                    tzinfo=tzinfo,
                )
            except ValueError:
                continue
    return None
