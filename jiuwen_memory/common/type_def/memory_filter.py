"""MemoryUnit 的公共 FilterExpr 求值语义。"""

from __future__ import annotations

from datetime import datetime

from .filter import FilterClause, FilterExpr, FilterOp, evaluate, filter_field_metadata_key
from .memory import T_EVENT_UNKNOWN, T_INVALID_OPEN, MemoryUnit


def _epoch_ms(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * 1000)


def _field_value(unit: MemoryUnit, field: str):
    if field == "tags":
        return unit.tags
    if field == "tier":
        return unit.tier.value
    if field == "source":
        return unit.source.value
    if field == "lifecycle":
        return unit.lifecycle.value
    if field in ("unit_id", "id"):
        return unit.id
    if field == "t_event":
        # 与索引投影对称：真源 None → 哨兵 T_EVENT_UNKNOWN，使事件窗 OR 组的 EQ 0
        # 分支在后置复核里同样成立（候选不被 is_retrieval_candidate 误砍）。
        # in_event_window 仍读真源 datetime、对 None/naive 放行，与此不冲突。
        value = _epoch_ms(unit.temporal.t_event)
        return T_EVENT_UNKNOWN if value is None else value
    if field == "t_valid":
        return _epoch_ms(unit.temporal.t_valid)
    if field == "t_invalid":
        value = _epoch_ms(unit.temporal.t_invalid)
        return T_INVALID_OPEN if value is None else value
    if field == "t_message":
        return _epoch_ms(unit.temporal.t_message)
    key = filter_field_metadata_key(field)
    if key.startswith("user_metadata."):
        return unit.user_metadata.get(key.removeprefix("user_metadata."))
    if key.startswith("system_metadata."):
        return unit.system_metadata.get(key.removeprefix("system_metadata."))
    return None


def _matches_clause(unit: MemoryUnit, clause: FilterClause) -> bool:
    value = _field_value(unit, clause.field)
    op, target = clause.op, clause.value

    if isinstance(value, (list, tuple, set)):  # 集合字段（如 tags）
        members = set(value)
        # 形态语义严格分离：CONTAINS 只命中数组成员，EQ / IN 只命中标量，
        # 两者不重叠；NE / NOT_IN 分别是后两者的逻辑否定，对数组恒真。
        if op is FilterOp.CONTAINS:
            return target in members
        if op is FilterOp.EQ:
            return False
        if op is FilterOp.NE:
            return True
        if op is FilterOp.IN:
            return False
        if op is FilterOp.NOT_IN:
            return True
        # 范围算子对集合无意义：判否，与下方标量不可比时的 TypeError 分支同答案。
        # 放行会让该谓词失效——真源复核是正确性边界，不可判定的组合不能当作不约束。
        return False

    if value is None:
        return op in (FilterOp.NE, FilterOp.NOT_IN)
    if op is FilterOp.EQ:
        return value == target
    if op is FilterOp.NE:
        return value != target
    if op is FilterOp.IN:
        return value in (target or [])
    if op is FilterOp.NOT_IN:
        return value not in (target or [])
    if op is FilterOp.CONTAINS:
        return False  # 标量既不做等值退化，也不做字符串子串匹配
    try:
        if op is FilterOp.GT:
            return value > target
        if op is FilterOp.GTE:
            return value >= target
        if op is FilterOp.LT:
            return value < target
        if op is FilterOp.LTE:
            return value <= target
    except TypeError:
        return False
    return True


def matches_memory_unit(unit: MemoryUnit, filters: FilterExpr | None) -> bool:
    """按统一字段投影和比较规则判断 MemoryUnit 是否满足过滤表达式。"""
    return evaluate(filters, lambda clause: _matches_clause(unit, clause))
