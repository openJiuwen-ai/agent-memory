"""Detailed-summary references help source selection within the same index quota."""

import json
from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY, EntityLabel, GraphSemantics
from jiuwen_memory.common.graphmem_structure import DETAIL_SPANS_KEY, encode_child_details
from jiuwen_memory.common.type_def import (
    FilterClause,
    HierarchyRole,
    RecallBatch,
    RecallChannel,
    RecallResult,
    ScoredMemoryUnit,
    ScoredUnit,
    Segment,
)
from jiuwen_memory.retrieval.graph_navigation import GraphNavigationPolicy
from jiuwen_memory.retrieval.graphmem_query import QueryNeeds
from jiuwen_memory.retrieval.graphmem_relation_plan import child_detail_hints
from jiuwen_memory.retrieval.graphmem_relational_navigation import plan_relational
from tests.unit.construction.graphmem_fixtures import HOME
from tests.unit.retrieval.test_graphmem_relational_navigation import event_reference_fixture

pytestmark = pytest.mark.unit


def fixture():
    domain, parent, children, query, policy, _, _ = event_reference_fixture()
    details = ["Alice bought a bike.", "Bob visited a museum.", "Alice read a book.",
               "Alice brought tea to the riverside picnic."]
    parent.segments = [Segment(content="span"), Segment(content="\n".join(details))]
    parent.system_metadata[FEATURES_KEY] = GraphSemantics(
        (EntityLabel("Alice", "person"),), ()).encode()
    parent.system_metadata[DETAIL_SPANS_KEY] = encode_child_details(parent, details)
    domain.update(HOME, [parent])
    return domain, parent, children, query, replace(policy, detail_source_hints=True)


def allowed_ids(filters):
    if isinstance(filters, FilterClause):
        return filters.value if filters.field == "unit_id" else []
    return [uid for child in filters.children for uid in allowed_ids(child)]


@pytest.mark.parametrize("mode", ["normal", "empty_index", "bad_backlink", "summary_only"])
def test_hint_preserves_index_gate_body_quota_and_original_provenance(monkeypatch, mode):
    domain, parent, children, query, policy = fixture()
    if mode == "bad_backlink":
        children[3].hierarchy.parent_id = "different-parent"
        domain.update(children[3].scope, [children[3]])
    if mode == "summary_only":
        policy = replace(policy, include_original=False)
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        ids = allowed_ids(parsed.scalar_filters)
        calls.append((scope, ids, recall_limit))
        chosen = [] if mode == "empty_index" else sorted(ids)[:recall_limit]
        return RecallResult([RecallBatch(channels[0], "l2", [ScoredUnit(uid, .8)
                                                               for uid in chosen])])

    monkeypatch.setattr(domain, "recall", recall)
    domain.reads.clear()
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    sources = [c for c in plan.candidates if c.unit.hierarchy.role is HierarchyRole.SNAPSHOT]
    assert len(sources) <= 2
    assert sum(len(ids) for scope, ids in domain.reads if scope.session) <= 2
    assert plan.diagnostics["event_source_candidates"] == 0
    if mode == "normal":
        assert {c.unit.id for c in sources} == {children[0].id, children[3].id}
        assert calls[0][1] == [children[3].id]
        assert plan.diagnostics["detail_source_candidates"] == 1
        link = next(e for e in plan.links if e["target"][-1] == children[3].id)
        assert link["reason"]["detail_references"] == [{"detail_position": 3, "clauses": ["C1"]}]
        assert link["reason"]["event_references"] == []
    else:
        assert children[3].id not in {c.unit.id for c in sources}
        if mode == "summary_only":
            assert not calls and not any(scope.session for scope, _ in domain.reads)


def test_details_do_not_join_unrelated_entities_sentences_or_override_calendar_windows():
    _, parent, children, query, _ = fixture()
    needs = query.navigation_needs
    args = children[0].scope, parent.hierarchy.child_ids, HierarchyRole.SNAPSHOT
    assert child_detail_hints(parent, needs, *args)
    assert not child_detail_hints(parent, replace(needs, entities=("Bob",)), *args)
    assert not child_detail_hints(parent, replace(needs, time_from="2023-05-01"), *args)
    assert not child_detail_hints(parent, needs, HOME, *args[1:])
    assert not child_detail_hints(parent, needs, *args[:2], HierarchyRole.TIME_SPAN)
    data = json.loads(parent.system_metadata[DETAIL_SPANS_KEY])
    data["binding"] = "stale"
    parent.system_metadata[DETAIL_SPANS_KEY] = json.dumps(data)
    assert not child_detail_hints(parent, needs, *args)


@pytest.mark.parametrize(("time_from", "expected"), [("", True), ("2024-01-01", False)])
def test_branch_clause_rewrite_changes_only_the_existing_keyword_request(
    monkeypatch, time_from, expected,
):
    domain, parent, children, query, policy, _, _ = event_reference_fixture()
    query = replace(query, vector=[1.0, 0.0], navigation_needs=QueryNeeds(
        "comparison", ("Alice",), ("riverside picnic", "books read"),
        time_from=time_from, analysis_status="used"))
    policy = replace(policy, source_limit=3, source_candidate_limit=3,
                     source_clause_keywords=True, semantic_source_terms=True)
    calls = []

    def recall(scope, parsed, *, channels, recall_limit):
        assert scope == children[0].scope and parsed.vector == query.vector
        ids = allowed_ids(parsed.scalar_filters)
        calls.append((channels[0], parsed.rewritten, recall_limit))
        assert set(ids) <= {child.id for child in children}
        return RecallResult([RecallBatch(channels[0], "l2",
            [ScoredUnit(uid, .8) for uid in sorted(ids)[-recall_limit:]])])

    monkeypatch.setattr(domain, "recall", recall)
    plan = plan_relational(domain, policy, HOME, query, [ScoredMemoryUnit(parent, 1)])
    assert bool(plan.diagnostics.get("source_clause_index_calls")) == expected
    assert sum(limit for _, _, limit in calls) == 3
    keywords = [text for channel, text, _ in calls if channel is RecallChannel.KEYWORD]
    assert len(keywords) == 1
    assert ("books read" not in keywords[0]) == expected
    assert all("books read" in text for channel, text, _ in calls
               if channel is RecallChannel.VECTOR)
    assert plan.diagnostics["source_candidates"] <= 3


def test_detail_option_is_explicit_and_relational_only():
    assert not GraphNavigationPolicy().detail_source_hints
    with pytest.raises(ValidationError):
        GraphNavigationPolicy(strategy="relational", detail_source_hints=1)
