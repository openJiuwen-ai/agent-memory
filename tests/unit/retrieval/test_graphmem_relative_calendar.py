"""Relative day checks use the query's clock and preserve unrelated API semantics."""

import json
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def.filter import FilterClause, FilterOp
from jiuwen_memory.retrieval.query_parser_impl.graphmem_query_parser import (
    GraphMemQueryParser,
    parse_needs,
)
from jiuwen_memory.retrieval.types import RetrievalQuery
from tests.unit.retrieval.test_query_parser import (
    _RecordingEmbedder,
    _RecordingLLM,
    _RecordingTokenizer,
)

pytestmark = pytest.mark.unit
CLOCK = "2024-03-06T16:30:00+00:00"
WRONG = {"from": "2024-03-02", "to": "2024-03-03"}


def response(window=WRONG, mode="fact"):
    return json.dumps({"mode": mode, "entities": ["Maya"],
                       "facets": ["joined workshop"], "time": window})


@pytest.mark.parametrize(("expression", "start", "end"), [
    ("last Friday", "2024-03-01T00:00:00+00:00", "2024-03-02T00:00:00+00:00"),
    ("next Monday", "2024-03-11T00:00:00+00:00", "2024-03-12T00:00:00+00:00"),
    ("last Wednesday", "2024-02-28T00:00:00+00:00", "2024-02-29T00:00:00+00:00"),
    ("yesterday", "2024-03-05T00:00:00+00:00", "2024-03-06T00:00:00+00:00"),
    ("today", "2024-03-06T00:00:00+00:00", "2024-03-07T00:00:00+00:00"),
    ("tomorrow", "2024-03-07T00:00:00+00:00", "2024-03-08T00:00:00+00:00"),
    ("six days ago", "2024-02-29T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),
    ("before yesterday", "", "2024-03-05T00:00:00+00:00"),
    ("after yesterday", "2024-03-06T00:00:00+00:00", ""),
    ("since yesterday", "2024-03-05T00:00:00+00:00", ""),
    ("as of yesterday", "", "2024-03-06T00:00:00+00:00"),
])
def test_point_dates_and_explicit_bounds_follow_the_reference_clock(expression, start, end):
    needs = parse_needs(response(), f"What did Maya join {expression}?", "", CLOCK,
                        resolve_relative_dates=True)
    assert (needs.time_from, needs.time_to) == (start, end)
    assert needs.entities == ("Maya",) and needs.facets == ("joined workshop",)
    assert needs.model_calls == 1


@pytest.mark.parametrize("question", [
    "What did Maya join last Friday, on 2024-01-05?",
    "What did Maya join last Friday or yesterday?",
    "What did Maya join last Friday and on Monday?",
    "What did Maya join about three days ago?",
    "What did Maya join 2.5 days ago?",
    "What did Maya join not on last Friday?",
    "What did Maya join yesterday morning?",
    "What did Maya join last Friday at 5 pm?",
    "What did Maya join last Friday at 17:30?",
    "What did Maya join the day after tomorrow?",
    "What did Maya join the day before yesterday?",
    "What did Maya join four weeks ago?",
    "What does Maya think of 'Yesterday'?",
    "What did Maya join 999999999999999999999 days ago?",
])
def test_ambiguous_or_unsupported_dates_keep_the_original_interpretation(question):
    needs = parse_needs(response(), question, "", CLOCK, resolve_relative_dates=True)
    assert (needs.time_from, needs.time_to) == (WRONG["from"], WRONG["to"])


@pytest.mark.parametrize(("clock", "expected"), [("", ""), ("not a clock", WRONG["from"])])
def test_missing_or_invalid_clock_never_uses_today(clock, expected):
    needs = parse_needs(response(), "What did Maya join last Friday?", "", clock,
                        resolve_relative_dates=True)
    assert needs.time_from == expected


def test_non_utc_calendar_day_and_equivalent_format_are_preserved():
    needs = parse_needs(response(), "What did Maya join yesterday?", "",
                        "2024-03-01T00:30:00+08:00", resolve_relative_dates=True)
    assert needs.time_from == "2024-02-28T16:00:00+00:00"
    assert needs.time_to == "2024-02-29T16:00:00+00:00"
    window = {"from": "2024-03-01", "to": "2024-03-02"}
    needs = parse_needs(response(window), "What did Maya join last Friday?", "", CLOCK,
                        resolve_relative_dates=True)
    assert (needs.time_from, needs.time_to) == (window["from"], window["to"])


def test_default_null_and_broad_state_interpretations_remain_unchanged():
    q = "What did Maya join last Friday?"
    assert parse_needs(response(), q, "", CLOCK).time_from == WRONG["from"]
    assert not parse_needs(response(None), q, "", CLOCK,
                           resolve_relative_dates=True).time_from
    window = {"from": "2024-02-01", "to": "2024-03-06"}
    assert parse_needs(response(window), q, "", CLOCK,
                       resolve_relative_dates=True).time_from == window["from"]
    assert parse_needs(response(mode="state"), q, "", CLOCK,
                       resolve_relative_dates=True).time_from == WRONG["from"]


def test_parser_preserves_original_calls_filters_and_valid_time():
    llm, embedder = _RecordingLLM(response()), _RecordingEmbedder()
    parser = GraphMemQueryParser(_RecordingTokenizer(), embedder, llm, None,
                                resolve_relative_dates=True)
    at = datetime(2025, 1, 1, tzinfo=UTC)
    filters = FilterClause("user_metadata.kind", FilterOp.EQ, "allowed")
    parsed = parser.parse(RetrievalQuery("What did Maya join last Friday?", as_of=at,
        filters=filters, extensions={"graphmem_reference_time": CLOCK}))
    assert parsed.navigation_needs.time_from == "2024-03-01T00:00:00+00:00"
    assert parsed.time_from is parsed.time_to is None
    assert parsed.as_of == at and parsed.scalar_filters == filters
    assert llm.calls == embedder.calls == 1


@pytest.mark.parametrize("value", [1, "true", None])
def test_option_requires_a_boolean(value):
    with pytest.raises(ValidationError, match="resolve_relative_dates must be bool"):
        GraphMemQueryParser(_RecordingTokenizer(), None, None, None,
                            resolve_relative_dates=value)
