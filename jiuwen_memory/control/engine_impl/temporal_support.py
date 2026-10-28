# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""三个 MemoryEngine 实现共用的时间比较真源：naive/aware 混合形态先归一再比。

契约层（CLI/MCP/HTTP 的 ``_parse_occurred_at``）与 SDK 进程内直调都按 ISO 8601
原样解析时间：不带时区后缀得到 naive ``datetime``，带后缀得到 aware ``datetime``，
两种形态都是合法输入；落盘 codec 也原样保留写入形态，因此存量数据里 naive 与 aware
可以并存。Python 语义下 naive 与 aware 直接比较恒抛 ``TypeError``——凡跨「入参时间」
（``before`` / ``as_of``）与「记忆时间」（``t_message`` / ``t_valid`` / ``t_invalid``）
的比较点，都必须双侧过 :func:`as_utc` 后再比：naive 按 UTC 解释，aware 保留实际时刻。
这与 ``common/type_def/retrieval_filter.valid_at`` 的既有约定一致（Storage/Retrieval
共用路径早已归一），本模块是控制层同一概念的唯一实现，三个引擎共用，不再各自复制。
"""

from __future__ import annotations

from datetime import UTC, datetime

from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.common.type_def.memory_filter import matches_memory_unit
from jiuwen_memory.control.types import DeleteSelector


def _utc(value: datetime | None) -> datetime | None:
    """归一到 UTC；``None`` 保持 ``None``（缺值语义由调用点处理）。"""
    return None if value is None else as_utc(value)


def valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    """``as_of`` 时刻该 unit 是否处于有效期内。

    非正有效期窗口（``t_invalid <= t_valid``）是引擎侧特例：此时只认
    ``as_of < t_invalid`` 为有效，与 ``retrieval_filter.valid_at`` 的通用语义不同，
    抽公共实现时保留该分支。
    """
    valid_from = _utc(unit.temporal.t_valid)
    invalid_from = _utc(unit.temporal.t_invalid)
    moment = as_utc(as_of)
    has_non_positive_validity_window = (
        valid_from is not None and invalid_from is not None and invalid_from <= valid_from
    )
    if has_non_positive_validity_window:
        return moment < invalid_from
    if valid_from is not None and moment < valid_from:
        return False
    if invalid_from is not None and moment >= invalid_from:
        return False
    return True


def matches_delete_selector(unit: MemoryUnit, selector: DeleteSelector) -> bool:
    """按删除选择器逐条判定命中：各条件取「与」。"""
    wanted_ids = set(selector.unit_ids)
    wanted_tags = set(selector.tags)
    if wanted_ids and unit.id not in wanted_ids:
        return False
    if wanted_tags and not wanted_tags.intersection(unit.tags):
        return False
    if selector.before is not None:
        t_message = unit.temporal.t_message
        if t_message is None or as_utc(t_message) >= as_utc(selector.before):
            return False
    if selector.filters is not None and not matches_memory_unit(unit, selector.filters):
        return False
    return True


def valid_sort_key(unit: MemoryUnit) -> datetime:
    """历史版本按 UTC 生效时间排序；缺值沿用最早时间哨兵。"""
    value = unit.temporal.t_valid
    return datetime.min.replace(tzinfo=UTC) if value is None else as_utc(value)
