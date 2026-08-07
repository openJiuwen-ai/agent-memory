"""Storage 与 Retrieval 共用的 MemoryUnit 真源复核纯函数。"""

from __future__ import annotations

from datetime import datetime, timezone

from .filter import FilterExpr
from .memory import LifecycleState, MemoryUnit
from .memory_filter import matches_memory_unit


def valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    temporal = unit.temporal
    if temporal.t_valid is not None and as_of < temporal.t_valid:
        return False
    if temporal.t_invalid is not None and as_of >= temporal.t_invalid:
        return False
    return True


def passes_lifecycle(
    unit: MemoryUnit, as_of: datetime | None, include_archived: bool = False
) -> bool:
    if as_of is None:
        allowed = {LifecycleState.ACTIVE}
        if include_archived:
            allowed.add(LifecycleState.ARCHIVED)
        return unit.lifecycle in allowed and valid_at(unit, datetime.now(timezone.utc))
    return unit.lifecycle != LifecycleState.FORGOTTEN and valid_at(unit, as_of)


def in_event_window(
    unit: MemoryUnit, time_from: datetime | None, time_to: datetime | None
) -> bool:
    if time_from is None and time_to is None:
        return True
    event_time = unit.temporal.t_event
    if event_time is None or event_time.tzinfo is None:
        return True
    if time_from is not None and event_time < time_from:
        return False
    return time_to is None or event_time < time_to


def matches_retrieval_filters(unit: MemoryUnit, filters: FilterExpr | None) -> bool:
    return matches_memory_unit(unit, filters)


def is_retrieval_candidate(
    unit: MemoryUnit,
    *,
    as_of: datetime | None,
    time_from: datetime | None,
    time_to: datetime | None,
    filters: FilterExpr | None,
    include_archived: bool,
) -> bool:
    return (
        passes_lifecycle(unit, as_of, include_archived)
        and in_event_window(unit, time_from, time_to)
        and matches_retrieval_filters(unit, filters)
    )
