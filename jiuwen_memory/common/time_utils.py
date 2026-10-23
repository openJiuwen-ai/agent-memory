# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆时间的公共解释规则：不依赖进程本地时区，不改写输入值。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# 绝对日期共享规则：查询侧解析时间窗与写入侧抽取事件时间用同一份正则，
# 保证同一自然年内、同一日期字符串在两侧解析到同一天——这是时间窗能命中的前提。
# 两种形态：带年（2026年6月8日 / 2026-06-08 / 2026/6/8 / 2025年的6月8日）；无年
# （6月8日/6月8号）。带年形态容忍「年」与月日之间的「的」/逗号/空白——否则显式
# 年份掉进无年形态会被 now.year 静默顶替为错年。无年形态前置断言排除数字与「年」：
# 不吃相邻数字、不把「2026年13月1日」这类非法带年日期截成「13月1日」重新解释；
# 尾部不断言数字——「6月8日14点」的日期提取不受后接时间数字影响（带年形态经
# 回溯本就能命中，两形态行为一致）。
_ABSOLUTE_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/年][的，,\s]*(\d{1,2})[-/月][的，,\s]*(\d{1,2})(?:[日号])?(?!\d)"),
    re.compile(r"(?<![\d年])(\d{1,2})月(\d{1,2})[日号]"),
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

    - 「第一个」按文本位置计、跨形态一致（「6月6日团建，2026年6月8日吃饭」取
      6月6日）。带年份的日期用显式年份；无年份按 ``now.year`` 补齐（跨年写入/
      查询时两侧各按当时年份解释，见上）。查询侧与写入侧共用本规则，同一日期
      字符串在同一自然年内解析到同一天；跨年指代（如 1 月说「去年6月」）属相对
      时间词，由查询侧相对规则负责，不在本函数处理。
    - 非法日期（6月32日、13月）跳过并尝试后续匹配；全部不可解析返回 ``None``。
    - 区间表述（「6月20日到21日」）取首日；多日期内容也只记首个——直存链路每个
      unit 只有一个 ``t_event``，其余日期的拆分归 EXTRACT 链路，跨度建模归时间树
      结构，此处不展开。
    """
    tzinfo = now.tzinfo or UTC
    matches: list[tuple[int, re.Match[str]]] = []
    for pattern in _ABSOLUTE_DATE_PATTERNS:
        matches.extend((match.start(), match) for match in pattern.finditer(text))
    # 按文本位置取首个；同位时分组多（带年）者优先——两形态起始本就不重叠，仅为稳定排序。
    matches.sort(key=lambda pair: (pair[0], -len(pair[1].groups())))
    for _position, match in matches:
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
