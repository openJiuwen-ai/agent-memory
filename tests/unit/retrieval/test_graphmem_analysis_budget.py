"""Optional analysis retains original entry points and a visible evidence reserve."""
import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.graph_navigation import GraphEvidenceNavigator, GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.query_parser_impl.graphmem_query_parser import GraphMemQueryParser
from jiuwen_memory.retrieval.types import RetrievalQuery
from tests.unit.construction.graphmem_fixtures import HOME, world
from tests.unit.retrieval.test_graphmem_query_closure import response
from tests.unit.retrieval.test_query_parser import (
    _RecordingEmbedder,
    _RecordingLLM,
    _RecordingTokenizer,
)

pytestmark = pytest.mark.unit


def test_personal_mode_avoids_an_extra_call_for_an_explicit_named_question():
    llm, embedder = _RecordingLLM(response()), _RecordingEmbedder()
    parser = GraphMemQueryParser(_RecordingTokenizer(), embedder, llm, None,
                                analysis_mode="personal")
    parsed = parser.parse(RetrievalQuery("What bicycle did Alice buy?"))
    assert parsed.raw == parsed.rewritten == "What bicycle did Alice buy?"
    assert llm.calls == 0 and embedder.calls == 1
    parsed = parser.parse(RetrievalQuery("What bicycle did I buy?"))
    assert llm.calls == 1 and parsed.navigation_needs.model_calls == 1
    assert parsed.navigation_needs.entities == ()


@pytest.mark.parametrize("calls,status,expected", [(0, "rules", 1024), (1, "used", 896),
                                                    (1, "fallback", 896)])
def test_reserve_applies_only_after_an_analysis_attempt(calls, status, expected):
    domain, _, _, _ = world([])
    policy = GraphNavigationPolicy(strategy="relational", analysis_token_reserve=128)
    query = GraphMemParsedQuery(raw="How many bicycles did I buy?", navigation_needs=QueryNeeds(
        "collection", facets=("bicycle purchase",), analysis_status=status, model_calls=calls))
    _, bundle = GraphEvidenceNavigator(domain, policy).pack_closure(
        HOME, query, [], EvidencePacker(len, "characters", mode="sources_first"),
        max_tokens=1024, top_k=16)
    assert bundle.token_budget == expected
    assert bundle.navigation["requested_token_limit"] == 1024
    assert bundle.navigation["effective_evidence_budget"] == expected
    assert bundle.navigation["analysis_reserved_tokens"] == 1024 - expected


def test_reserve_cannot_consume_the_whole_evidence_budget():
    domain, _, _, _ = world([])
    policy = GraphNavigationPolicy(strategy="relational", analysis_token_reserve=1024)
    query = GraphMemParsedQuery(raw="my bike", navigation_needs=QueryNeeds(model_calls=1))
    with pytest.raises(ValidationError, match="smaller"):
        GraphEvidenceNavigator(domain, policy).pack_closure(
            HOME, query, [], EvidencePacker(len, "characters", mode="sources_first"),
            max_tokens=1024, top_k=16)
