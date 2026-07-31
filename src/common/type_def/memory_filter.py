"""MemoryUnit 的公共 FilterExpr 求值语义。"""

from __future__ import annotations

from datetime import datetime

from .filter import FilterClause, FilterExpr, FilterOp, evaluate, filter_field_metadata_key
from .memory import T_INVALID_OPEN, MemoryUnit


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
        return _epoch_ms(unit.temporal.t_event)
    if field == "t_valid":
        return _epoch_ms(unit.temporal.t_valid)
    if field == "t_invalid":
        value = _epoch_ms(unit.temporal.t_invalid)
        return T_INVALID_OPEN if value is None else value
    return unit.metadata.get(filter_field_metadata_key(field))


def _matches_clause(unit: MemoryUnit, clause: FilterClause) -> bool:
    value = _field_value(unit, clause.field)
    op, target = clause.op, clause.value

    if isinstance(value, (list, tuple, set)):
        members = set(value)
        if op in (FilterOp.CONTAINS, FilterOp.EQ):
            return target in members
        if op is FilterOp.NE:
            return target not in members
        if op is FilterOp.IN:
            return bool(members & set(target or []))
        if op is FilterOp.NOT_IN:
            return not (members & set(target or []))
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
        return value == target
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
