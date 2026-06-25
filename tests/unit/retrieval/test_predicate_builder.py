"""PredicateBuilder: system pre-filter clauses from lifecycle/as_of/time."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


def test_event_time_window_emits_range() -> None:
    ops = {(c.field, c.op) for c in build_system_filters(None, NOW, NOW)}

    assert ("t_event", FilterOp.GTE) in ops
    assert ("t_event", FilterOp.LTE) in ops
