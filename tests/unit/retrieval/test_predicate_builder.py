"""PredicateBuilder: system pre-filter clauses from lifecycle/as_of/time."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.type_def import T_INVALID_OPEN
from common.type_def.filter import FilterOp
from retrieval.retriever_impl.predicate_builder import build_system_filters

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


def test_event_time_window_emits_half_open_range() -> None:
    """事件时间窗是半开区间 [time_from, time_to)，与 in_event_window 后置复核同边界。"""
    ops = {(c.field, c.op) for c in build_system_filters(None, NOW, NOW)}

    assert ("t_event", FilterOp.GTE) in ops
    assert ("t_event", FilterOp.LT) in ops
    assert ("t_event", FilterOp.LTE) not in ops
