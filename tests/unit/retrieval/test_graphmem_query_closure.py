"""Optional query analysis and bounded packing retain scope and provenance."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.common.type_def.filter import FilterClause, FilterOp
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
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


def response(**extra):
    return json.dumps(
        {"mode": "fact", "entities": ["Alice"], "facets": ["bicycle"], "time": None, **extra}
    )


def query(*, mode="fact", facets=("bicycle",)):
    return GraphMemParsedQuery(
        raw="What bicycle did Alice buy?",
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        navigation_needs=QueryNeeds(
            mode, ("Alice",), facets, analysis_status="used", model_calls=1
        ),
    )


def test_analysis_keeps_raw_vector_entry_and_hard_filters_with_one_bounded_call():
    llm, embedder = _RecordingLLM(response()), _RecordingEmbedder()
    parser = GraphMemQueryParser(_RecordingTokenizer(), embedder, llm, None)
    filters = FilterClause("user_metadata.kind", FilterOp.EQ, "allowed")
    clock = datetime(2023, 1, 1, tzinfo=UTC)
    parsed = parser.parse(
        RetrievalQuery("What bicycle did Alice buy?", filters=filters, as_of=clock)
    )
    assert parsed.raw == parsed.rewritten == "What bicycle did Alice buy?"
    assert parsed.scalar_filters == filters and parsed.as_of == clock
    assert parsed.navigation_needs.entities == ("Alice",)
    assert llm.calls == 1 and embedder.calls == 1
    assert llm.options[0]["max_tokens"] == 384
    assert replace(parsed, hierarchy_role=None).navigation_needs == parsed.navigation_needs


def test_invalid_analysis_falls_back_once_without_erasing_retrieval():
    llm = _RecordingLLM("not JSON")
    parser = GraphMemQueryParser(_RecordingTokenizer(), _RecordingEmbedder(), llm, None)
    parsed = parser.parse(RetrievalQuery("List all Alice's bikes"))
    assert llm.calls == 1 and parsed.vector
    assert parsed.navigation_needs.analysis_status == "fallback"
    assert parsed.navigation_needs.mode == "collection"


def test_explicit_collection_cannot_be_demoted_to_early_stop_by_model():
    llm = _RecordingLLM(response(mode="fact"))
    parser = GraphMemQueryParser(_RecordingTokenizer(), _RecordingEmbedder(), llm, None)
    parsed = parser.parse(RetrievalQuery("How many bicycles did I buy?"))
    assert parsed.navigation_needs.mode == "collection"
    assert parsed.navigation_needs.analysis_status == "used_rules_guard"
    assert llm.calls == 1


def test_ungrounded_names_and_unanchored_calendar_guesses_are_not_hard_filters():
    parsed = parse_needs(
        response(entities=["Alice", "Bob"], time={"from": "2026-11-01", "to": "2026-12-01"}),
        "What bicycle did Alice buy last month?",
        "",
        "",
    )
    assert parsed.entities == ("Alice",)
    assert not parsed.time_from and not parsed.time_to
    with pytest.raises(ValueError):
        parse_needs(response(facets=["x"] * 5), "question", "", "")


def test_pronouns_and_roles_are_not_named_entities_but_named_phrases_survive():
    needs = parse_needs(response(entities=["I", "user", "Alice", "I, Robot"]),
        "Did I recommend I, Robot to Alice?", "user", "")
    assert needs.entities == ("Alice", "I, Robot")
