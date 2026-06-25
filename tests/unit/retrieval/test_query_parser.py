"""Query parser contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.type_def.filter import FilterClause, FilterOp
from retrieval.types import RecallChannel, RetrievalQuery

pytestmark = pytest.mark.unit


def test_parser_transfers_filters_and_as_of(world) -> None:
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    filters = [FilterClause("tags", FilterOp.CONTAINS, "x")]

    parsed = world.parser.parse(RetrievalQuery(text="hello world", filters=filters, as_of=as_of))

    assert parsed.scalar_filters == filters
    assert parsed.as_of == as_of
    assert parsed.tokens == ["hello", "world"]
    assert parsed.vector
    assert RecallChannel.KEYWORD in parsed.channels
    assert RecallChannel.VECTOR in parsed.channels


def test_parser_extracts_time_constraint(world) -> None:
    parsed = world.parser.parse(RetrievalQuery(text="昨天 coffee"))

    assert parsed.time_from is not None
    assert parsed.time_to is not None
