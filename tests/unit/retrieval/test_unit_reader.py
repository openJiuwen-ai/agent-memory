"""UnitReader（scope 内点读）+ 后置过滤（passes / in_event_window / matches_filters）测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from common.type_def import FilterClause, FilterOp
from common.type_def.memory import LifecycleState
from common.type_def.memory_codec import dumps
from retrieval.retriever_impl.unit_reader import (
    UnitReader,
    in_event_window,
    matches_filters,
    passes,
)
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


def test_current_query_allows_active_only(unit_factory) -> None:
    assert passes(unit_factory("a", "x", lifecycle=LifecycleState.ACTIVE), None)
    assert not passes(unit_factory("s", "x", lifecycle=LifecycleState.SUPERSEDED), None)
    assert not passes(unit_factory("r", "x", lifecycle=LifecycleState.ARCHIVED), None)


def test_include_archived_allows_archived(unit_factory) -> None:
    unit = unit_factory("r", "x", lifecycle=LifecycleState.ARCHIVED)

    assert passes(unit, None, include_archived=True)


def test_historical_query_excludes_forgotten(unit_factory) -> None:
    assert not passes(unit_factory("f", "x", lifecycle=LifecycleState.FORGOTTEN), NOW)
    assert passes(unit_factory("a", "x", lifecycle=LifecycleState.ACTIVE), NOW)


def test_valid_time_window_excludes_not_yet_effective(unit_factory) -> None:
    future = unit_factory("u", "x", t_valid=NOW + timedelta(days=1))

    assert not passes(future, NOW)


# -- event-time 窗（in_event_window） ----------------------------------------- #


def test_event_window_filters_by_t_event(unit_factory) -> None:
    lo, hi = NOW - timedelta(days=1), NOW + timedelta(days=1)
    inside = unit_factory("in", "x", t_event=NOW)
    before = unit_factory("be", "x", t_event=NOW - timedelta(days=3))

    assert in_event_window(inside, lo, hi)
    assert not in_event_window(before, lo, hi)


def test_event_window_is_half_open(unit_factory) -> None:
    lo, hi = NOW - timedelta(days=1), NOW
    on_upper = unit_factory("u", "x", t_event=NOW)  # 上界 exclusive

    assert not in_event_window(on_upper, lo, hi)


def test_event_window_lenient_when_no_t_event(unit_factory) -> None:
    # 缺 t_event：不据此丢弃（宽松兜底，避免 over-drop）
    assert in_event_window(unit_factory("n", "x"), NOW - timedelta(days=1), NOW + timedelta(days=1))


def test_event_window_noop_without_constraint(unit_factory) -> None:
    assert in_event_window(unit_factory("a", "x", t_event=NOW), None, None)


# -- 调用方显式 filters（matches_filters） ------------------------------------ #


def test_filter_tags_contains(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["work", "coffee"])

    assert matches_filters(unit, [FilterClause("tags", FilterOp.CONTAINS, "coffee")])
    assert not matches_filters(unit, [FilterClause("tags", FilterOp.CONTAINS, "tea")])


def test_filter_scalar_field_eq_and_in(unit_factory) -> None:
    unit = unit_factory("a", "x")  # tier=SEMANTIC（conftest make_unit 固定）

    assert matches_filters(unit, [FilterClause("tier", FilterOp.EQ, "semantic")])
    assert matches_filters(unit, [FilterClause("tier", FilterOp.IN, ["semantic", "core"])])
    assert not matches_filters(unit, [FilterClause("tier", FilterOp.EQ, "core")])


def test_filter_missing_field_only_negative_ops_pass(unit_factory) -> None:
    unit = unit_factory("a", "x")  # 无 "priority" metadata

    assert matches_filters(unit, [FilterClause("priority", FilterOp.NE, "high")])
    assert not matches_filters(unit, [FilterClause("priority", FilterOp.EQ, "high")])


def test_filter_and_combination(unit_factory) -> None:
    unit = unit_factory("a", "x", tags=["coffee"])

    assert matches_filters(
        unit,
        [
            FilterClause("tags", FilterOp.CONTAINS, "coffee"),
            FilterClause("tier", FilterOp.EQ, "semantic"),
        ],
    )
    assert not matches_filters(
        unit,
        [
            FilterClause("tags", FilterOp.CONTAINS, "coffee"),
            FilterClause("tier", FilterOp.EQ, "core"),  # 一条不满足 → 整体不通过
        ],
    )


# -- scope 内点读（load） ------------------------------------------------------ #


def test_unit_reader_loads_by_id_in_scope(scope, unit_factory) -> None:
    kv = InMemoryKVStore()
    kv.insert(scope, "u1", dumps(unit_factory("u1", "hello")))
    reader = UnitReader(kv)

    loaded = reader.load(scope, ["u1", "missing"])

    assert set(loaded) == {"u1"}
    assert loaded["u1"].content == "hello"
