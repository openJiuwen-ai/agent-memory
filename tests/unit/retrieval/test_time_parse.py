"""Rule-based query time-constraint parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from retrieval.query_parser_impl.time_parse import parse_time

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


def test_yesterday_returns_day_bounds() -> None:
    lo, hi = parse_time("昨天的会议", now=NOW)

    assert lo == datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert hi == datetime(2026, 6, 16, tzinfo=timezone.utc)


def test_recent_n_days_window_ends_now() -> None:
    lo, hi = parse_time("最近3天", now=NOW)

    assert lo == NOW - timedelta(days=3)
    assert hi == NOW


def test_no_time_expression_returns_none() -> None:
    assert parse_time("just coffee", now=NOW) == (None, None)
