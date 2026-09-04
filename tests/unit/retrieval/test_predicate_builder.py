"""PredicateBuilder: system pre-filter clauses from lifecycle/as_of/time."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.type_def import T_EVENT_UNKNOWN, T_INVALID_OPEN
from jiuwen_memory.common.type_def.filter import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    iter_clauses,
)
from jiuwen_memory.retrieval.retriever_impl.predicate_builder import build_system_filters

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


def test_current_query_emits_active_in() -> None:
    clauses = build_system_filters(None, None, None)

    lifecycle = [c for c in clauses if c.field == "lifecycle"][0]
    assert lifecycle.op == FilterOp.IN
    assert lifecycle.value == ["active"]


def test_include_archived_widens_lifecycle() -> None:
    clauses = build_system_filters(None, None, None, include_archived=True)

    lifecycle = [c for c in clauses if c.field == "lifecycle"][0]
    assert set(lifecycle.value) == {"active", "archived"}


def test_historical_query_emits_time_and_not_forgotten() -> None:
    fields = {(c.field, c.op) for c in build_system_filters(NOW, None, None)}

    assert ("lifecycle", FilterOp.NE) in fields
    assert ("t_valid", FilterOp.LTE) in fields
    assert ("t_invalid", FilterOp.GT) in fields


def test_t_invalid_pushdown_matches_index_sentinel() -> None:
    """t_invalid 下推的阈值必须小于索引哨兵，否则开放区间记忆会被整批排他。

    索引投影对真源 t_invalid=None 落 T_INVALID_OPEN（见 index_builder），本谓词
    `t_invalid > as_of` 才对"永久有效"成立。两处是一套约定，改一处即破。
    """
    clause = [c for c in build_system_filters(NOW, None, None) if c.field == "t_invalid"][0]

    assert clause.op == FilterOp.GT
    assert clause.value < T_INVALID_OPEN, "as_of 阈值超过哨兵时，开放区间记忆将被排他"


def test_event_time_window_emits_or_group_with_unknown_sentinel_branch() -> None:
    """事件窗下推为 OR(AND(GTE, LT), EQ T_EVENT_UNKNOWN)，未知时间 unit 不被窗下推清空。

    - 顶层为 FilterGroup(OR, ...)：一半开区间 [time_from, time_to) 的 AND 子组
      放行窗内已知事件；EQ T_EVENT_UNKNOWN 子树放行 t_event=None 的派生（索引里
      落哨兵 0）。后者是 F07 净化的刻意取舍——未知时间 ≠ 窗外。
    - 深度遍历 leaves 仍见 GTE / LT，半开边界与 in_event_window 后置复核同源；
      LTE 不得出现（会把 t_event==time_to 的记忆召回后再被复核砍掉）。
    """
    from datetime import timedelta

    time_from = NOW
    time_to = NOW + timedelta(days=1)
    clauses = build_system_filters(None, time_from, time_to)

    # 事件窗元素是顶层 OR FilterGroup（不再是扁平 FilterClause）
    event_exprs = [c for c in clauses if isinstance(c, FilterGroup)]
    assert len(event_exprs) == 1, "事件窗应为单个 OR FilterGroup"
    or_group = event_exprs[0]
    assert or_group.logic is FilterLogic.OR
    assert len(or_group.children) == 2

    # 子 1：AND(GTE?, LT?)；子 2：EQ T_EVENT_UNKNOWN
    and_group, eq_clause = or_group.children
    assert isinstance(and_group, FilterGroup) and and_group.logic is FilterLogic.AND
    assert isinstance(eq_clause, FilterClause)
    assert eq_clause.field == "t_event"
    assert eq_clause.op == FilterOp.EQ
    assert eq_clause.value == T_EVENT_UNKNOWN

    # 深度遍历 leaves：GTE / LT 在，LTE 不在
    ops = {
        (clause.field, clause.op)
        for clause in iter_clauses(or_group)
    }
    assert ("t_event", FilterOp.GTE) in ops
    assert ("t_event", FilterOp.LT) in ops
    assert ("t_event", FilterOp.LTE) not in ops


def test_event_time_window_only_from_emits_or_group_with_unknown_branch() -> None:
    """仅 time_from（time_to=None）时，AND 子组只含 GTE 一叶；OR 仍含 EQ 哨兵分支。"""
    clauses = build_system_filters(None, NOW, None)

    or_groups = [c for c in clauses if isinstance(c, FilterGroup)]
    assert len(or_groups) == 1
    or_group = or_groups[0]
    assert or_group.logic is FilterLogic.OR
    and_group, eq_clause = or_group.children
    assert isinstance(and_group, FilterGroup) and and_group.logic is FilterLogic.AND
    assert len(and_group.children) == 1, "仅 time_from → AND 子组只含 GTE"
    assert and_group.children[0].op is FilterOp.GTE
    assert eq_clause.value == T_EVENT_UNKNOWN


def test_no_time_window_emits_no_or_group() -> None:
    """无 time_from / time_to 时不应构造 OR 组——只返回 lifecycle/as_of clauses。"""
    clauses = build_system_filters(None, None, None)
    assert not any(isinstance(c, FilterGroup) for c in clauses)
