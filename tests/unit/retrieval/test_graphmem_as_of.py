"""Cumulative calendar cutoffs remain distinct from event and API valid-time windows."""

import json
from datetime import UTC, datetime

import pytest

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


def response(window=None):
    return json.dumps({"mode": "collection", "entities": ["Maya"],
                       "facets": ["completed training"], "time": window})


@pytest.mark.parametrize(("date_text", "end"), [
    ("December 2024", "2025-01-01"), ("2024", "2025-01-01"),
    ("29 February, 2024", "2024-03-01"), ("Feb. 29th, 2024", "2024-03-01"),
    ("2024-02-29", "2024-03-01"), ("2024-02", "2024-03-01"),
])
@pytest.mark.parametrize("window", [None, {"from": "2024-12-01", "to": "2024-12-31"}])
def test_explicit_as_of_includes_prior_history_and_full_calendar_unit(date_text, end, window):
    question = f"How many courses has Maya completed, as of {date_text}?"
    needs = parse_needs(response(window), question, "", "")
    assert not needs.time_from and needs.time_to == end
    assert needs.mode == "collection" and needs.model_calls == 1


@pytest.mark.parametrize("question", [
    "What did Maya complete in December 2024?",
    "What did Maya complete since March 2024, as of December 2024?",
    "What did Maya complete since June, as of December 2024?",
    "How many courses has Maya completed as of yesterday?",
    "How many courses has Maya completed as of 31 February, 2024?",
    "How many courses has Maya completed as of December 9999?",
    "How many courses as of 2024-01-01 and as of 2024-12-01?",
])
def test_ambiguous_invalid_or_multiple_calendars_do_not_invent_a_cutoff(question):
    needs = parse_needs(response(), question, "", "")
    assert not needs.time_from and not needs.time_to


def test_ordinary_event_interval_is_preserved():
    needs = parse_needs(response({"from": "2024-05-01", "to": "2024-06-01"}),
                        "Which courses did Maya complete in May 2024?", "", "")
    assert (needs.time_from, needs.time_to) == ("2024-05-01", "2024-06-01")


def test_soft_cutoff_does_not_change_filters_valid_time_or_model_call_count():
    llm, embedder = _RecordingLLM(response()), _RecordingEmbedder()
    parser = GraphMemQueryParser(_RecordingTokenizer(), embedder, llm, None)
    clock = datetime(2025, 2, 1, tzinfo=UTC)
    filters = FilterClause("user_metadata.kind", FilterOp.EQ, "allowed")
    parsed = parser.parse(RetrievalQuery("How many courses as of December 2024?",
                                        filters=filters, as_of=clock))
    assert parsed.as_of == clock and parsed.scalar_filters == filters
    assert parsed.time_from is parsed.time_to is None
    assert not parsed.navigation_needs.time_from
    assert parsed.navigation_needs.time_to == "2025-01-01"
    assert llm.calls == embedder.calls == 1
