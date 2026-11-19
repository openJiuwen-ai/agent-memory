"""Adaptive graph context keeps source attribution and one bounded read plan."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import ScoredMemoryUnit, Segment
from jiuwen_memory.retrieval import graphmem_relational_navigation as navigation
from jiuwen_memory.retrieval.evidence_packing import EvidencePacker
from jiuwen_memory.retrieval.expander import node_key
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_budget import (
    complete_source_parts,
    pack_adaptive_graph,
    source_support,
)
from jiuwen_memory.retrieval.graphmem_evidence_graph import pack_graph
from jiuwen_memory.retrieval.graphmem_query import GraphMemParsedQuery, QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import compile_program
from tests.unit.construction.graphmem_fixtures import HOME, tree

pytestmark = pytest.mark.unit


def sample(texts, *, facets=("bought bicycle",), mode="fact"):
    question = GraphMemParsedQuery(raw="What bicycle did Alice buy?", navigation_needs=QueryNeeds(
        mode, ("Alice",), facets, analysis_status="used", model_calls=1))
    plan = navigation.RelationalEvidencePlan(program=compile_program(question).describe())
    sources = []
    for i, text in enumerate(texts):
        parent, source = tree(f"p{i}", f"s{i}", text="Alice bought a bicycle.", raw=text)
        plan.candidates.append(ScoredMemoryUnit(parent, 1))
        sources.append(ScoredMemoryUnit(source, 1))
        plan.reasons[node_key(parent)] = {"selected_by": "initial_vector_keyword_match"}
        plan.links.append({"source": node_key(parent), "target": node_key(source),
                           "kind": "contains", "reason": {"verified_child_link": True}})
    plan.candidates.extend(sources)
    packer = EvidencePacker(len, "characters", mode="sources_first", header_style="compact",
                            max_summaries=0)
    policy = GraphNavigationPolicy(strategy="relational", adaptive_budget=True,
                                   initial_tokens=1024)
    return plan, question, packer, policy, sources


def test_simple_source_stops_without_packing_irrelevant_large_background():
    plan, question, packer, policy, sources = sample([
        "Alice bought a bicycle.", "Unrelated background " * 150])
    selected, bundle = pack_adaptive_graph(plan, packer, policy, question,
                                          max_tokens=8000, top_k=32)
    assert sources[0] in selected and sources[1] not in selected
    assert bundle.navigation["stopping_reason"] == "bounded_signals_saturated"
    assert len(bundle.navigation["stages"]) == 1
    assert bundle.token_count == len(bundle.rendered_context) <= bundle.token_budget == 1024


def test_unselected_relevant_source_expands_budget_over_same_plan():
    plan, question, packer, policy, sources = sample([
        "Alice bought a bicycle. " + "Background " * 100,
        "Alice bought a helmet. " + "Background " * 100],
        facets=("bought bicycle", "bought helmet"))
    selected, bundle = pack_adaptive_graph(plan, packer, policy, question,
                                          max_tokens=8000, top_k=32)
    assert all(c in selected for c in sources)
    stages = bundle.navigation["stages"]
    assert len(stages) > 1 and stages[0]["budget"] == 1024
    assert stages[-1]["budget"] < 8000
    assert bundle.navigation["query_coverage"]["remaining_signal_sources"] == 0
    assert bundle.navigation["stopping_reason"] == "bounded_signals_saturated"


def test_soft_support_can_saturate_without_requiring_every_query_word():
    plan, question, packer, policy, _ = sample(["Alice bought a red mountain bike."],
        facets=("bought red mountain bicycle",))
    _, bundle = pack_adaptive_graph(plan, packer, policy, question, max_tokens=8000, top_k=32)
    assert bundle.navigation["query_coverage"]["facet_coverage"] == [0.75]
    assert bundle.navigation["query_coverage"]["minimum_source_support"] == 0.75
    assert bundle.navigation["stopping_reason"] == "bounded_signals_saturated"


@pytest.mark.parametrize("mode", ["collection", "comparison", "temporal", "state", "multi_step"])
def test_complex_queries_use_full_budget(mode):
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."])
    question.navigation_needs = replace(question.navigation_needs, mode=mode)
    plan.program = compile_program(question).describe()
    _, bundle = pack_adaptive_graph(plan, packer, policy, question, max_tokens=8000, top_k=32)
    assert bundle.token_budget == 8000
    assert len(bundle.navigation["stages"]) == 1
    assert not bundle.navigation["adaptive_eligible"]


@pytest.mark.parametrize("text", ["Alice bought a bicycle?", "Unrelated source."])
def test_questions_or_parent_summary_cannot_prove_source_coverage(text):
    plan, question, packer, policy, _ = sample([text])
    _, bundle = pack_adaptive_graph(plan, packer, policy, question, max_tokens=8000, top_k=32)
    assert not bundle.navigation["query_coverage"]["covered"]
    assert bundle.token_budget == 8000


def test_source_signal_requires_sentence_local_entity_or_explicit_speaker():
    plan, question, _, _, sources = sample(["Bob bought a bicycle. Alice read a book."])
    unit = sources[0].unit
    assert source_support(unit, question.navigation_needs, False) == [0]
    unit.user_metadata["speaker"] = "Alice"
    assert source_support(unit, question.navigation_needs, False) == [0]
    unit.segments = [Segment(content="I bought a bicycle.")]
    assert source_support(unit, question.navigation_needs, False) == [1]
    unit.user_metadata["speaker"] = "assistant"
    assert source_support(unit, replace(question.navigation_needs, entities=()), True) == [0]


def test_fragment_completeness_and_scope_are_checked_before_early_stop():
    _, _, _, _, sources = sample(["Alice bought a bicycle.", "Alice bought a bicycle."])
    for i, item in enumerate(sources, 1):
        item.unit.source_ref = "same-payload"
        item.unit.system_metadata.update(_part=i, _parts=2)
    assert not complete_source_parts(sources), "Different sessions cannot complete one payload"
    sources[1].unit.scope = sources[0].unit.scope
    assert complete_source_parts(sources)
    assert not complete_source_parts(sources[:1])
    sources[1].unit.system_metadata["_part"] = True
    assert not complete_source_parts(sources)


@pytest.mark.parametrize("limiter", ["request_read_limited", "source_scope_limited"])
def test_incomplete_plan_cannot_trigger_early_stop(limiter):
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."])
    plan.diagnostics[limiter] = True
    _, bundle = pack_adaptive_graph(plan, packer, policy, question, max_tokens=8000, top_k=32)
    assert bundle.navigation["stopping_reason"] == "budget_limit"


def test_adaptive_public_wrapper_builds_only_one_plan_and_reserves_analysis(monkeypatch):
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."])
    calls = []

    def one_plan(*args, **kwargs):
        calls.append(1)
        return plan

    monkeypatch.setattr(navigation, "plan_relational", one_plan)
    _, bundle = navigation.pack_relational(None, replace(policy, analysis_token_reserve=768),
        HOME, question, [], packer, max_tokens=6144, top_k=32)
    assert calls == [1]
    assert bundle.navigation["analysis_reserved_tokens"] == 768
    assert bundle.navigation["effective_evidence_budget"] == 6144 - 768
    assert bundle.token_count == len(bundle.rendered_context) <= bundle.token_budget


def test_disabled_adaptation_preserves_fixed_graph_result(monkeypatch):
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."])
    expected, previous = pack_graph(plan, packer, max_tokens=8000, top_k=32,
                                   summary_tokens=policy.summary_token_budget)
    monkeypatch.setattr(navigation, "plan_relational", lambda *a, **kw: plan)
    selected, bundle = navigation.pack_relational(None, replace(policy, adaptive_budget=False),
        HOME, question, [], packer, max_tokens=8000, top_k=32)
    assert selected == expected
    assert bundle.rendered_context == previous.rendered_context
    assert bundle.sources == previous.sources and bundle.token_count == previous.token_count
    assert bundle.navigation["stopping_reason"] == "bounded_relation_plan"


def test_summary_only_keeps_full_budget_and_does_not_infer_raw_saturation():
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."])
    plan.candidates = plan.candidates[:1]
    _, bundle = pack_adaptive_graph(plan, packer, replace(policy, include_original=False),
                                    question, max_tokens=8000, top_k=32)
    assert not bundle.navigation["adaptive_eligible"]
    assert not bundle.navigation["query_coverage"]["covered"]
    assert bundle.token_budget == 8000


@pytest.mark.parametrize("value", [1, "true", None])
def test_adaptive_policy_requires_boolean(value):
    with pytest.raises(ValidationError, match="adaptive_budget"):
        GraphNavigationPolicy(strategy="relational", adaptive_budget=value)


def test_adaptive_policy_cannot_silently_affect_other_strategies():
    with pytest.raises(ValidationError, match="relational"):
        GraphNavigationPolicy(strategy="evidence", adaptive_budget=True)


@pytest.mark.parametrize(("mode", "tier", "ceiling"), [
    ("state", "standard", 6144), ("collection", "expanded", 7160),
    ("comparison", "multi_evidence", 10208), ("multi_step", "multi_evidence", 10208),
])
def test_relation_plan_controls_expanded_evidence_budget(mode, tier, ceiling):
    plan, question, packer, policy, _ = sample(["Alice bought a bicycle."], mode=mode)
    _, bundle = pack_adaptive_graph(plan, packer, replace(policy, adaptive_target_tokens=6144),
                                    question, max_tokens=10208, top_k=32)
    assert bundle.navigation["evidence_budget_tier"] == tier
    assert bundle.token_budget == bundle.navigation["tier_token_limit"] == ceiling
