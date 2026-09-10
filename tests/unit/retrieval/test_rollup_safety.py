"""MaxP、完整身份、逐边可见性和只读有界遍历。"""

from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwen_memory.common.type_def import (
    ChannelEvidence,
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    RecallChannel,
    ScoredMemoryUnit,
    normalize,
)
from jiuwen_memory.retrieval.retriever_impl.hierarchy_rollup import rollup_candidates
from tests.unit.retrieval.expansion_fixtures import link
from tests.unit.retrieval.hierarchy_query_fixtures import QUERY_SCOPE, SPAN_START, tree_unit
from tests.unit.retrieval.rollup_fixtures import read_spy, rollup_input

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("parent_score", [0.1, 0.95])
def test_maxp_keeps_best_direct_or_descendant_score_without_sum(parent_score) -> None:
    parent = tree_unit("parent")
    children = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(2)]
    link(parent, children)
    evidence = [ChannelEvidence(channel=RecallChannel.KEYWORD, score=parent_score)]
    direct = ScoredMemoryUnit(parent, parent_score, evidence=evidence)
    sources = [direct, ScoredMemoryUnit(children[0], 0.8), ScoredMemoryUnit(children[1], 0.7)]
    store = read_spy([parent])
    outcome = rollup_candidates(store, rollup_input(sources))
    assert len(outcome.candidates) == 1
    assert outcome.candidates[0].score == max(parent_score, 0.8)
    assert outcome.candidates[0].evidence == evidence
    assert direct.score == parent_score
    assert outcome.boosted_count == int(parent_score < 0.8)
    assert outcome.admitted_count == 0
    store.get.assert_not_called()


def test_new_parent_has_no_fabricated_direct_evidence_and_reads_are_cached() -> None:
    parent = tree_unit("parent")
    children = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(3)]
    link(parent, children)
    sources = [ScoredMemoryUnit(child, 0.8, evidence=[ChannelEvidence()]) for child in children]
    store = read_spy([parent])
    outcome = rollup_candidates(store, rollup_input(sources))
    assert outcome.admitted_count == 1
    assert outcome.candidates[0].evidence == []
    assert outcome.candidates[0].score == 0.8
    assert outcome.visited_count == 3
    store.get.assert_called_once_with(QUERY_SCOPE, [parent.id])


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_forbidden_parent_scope_is_never_read(dimension) -> None:
    parent = tree_unit("forbidden")
    child = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    boundary = replace(QUERY_SCOPE, session="allowed")
    child.scope = boundary
    parent.scope = replace(boundary, **{dimension: "other"})
    link(parent, [child])
    store = read_spy([parent])
    outcome = rollup_candidates(store, rollup_input([ScoredMemoryUnit(child, 0.8)], scope=boundary))
    assert outcome.candidates == []
    assert outcome.issues == ["scope_excluded"]
    store.get.assert_not_called()


@pytest.mark.parametrize("failure", [
    "missing", "wrong_payload_scope", "wrong_kind", "dismissed", "archived", "forgotten",
    "expired", "invalid_span", "uncovered_span", "wrong_backref", "wrong_child_scope",
    "filtered", "event_window", "read_error",
])
def test_unsafe_parent_is_excluded_without_backend_details(failure) -> None:
    parent = tree_unit("parent")
    child = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    link(parent, [child])
    request = rollup_input([ScoredMemoryUnit(child, 0.8)])
    if failure == "wrong_kind":
        parent.hierarchy.kind = HierarchyKind.TOPIC
    elif failure == "dismissed":
        parent.hierarchy.status = HierarchyStatus.DISMISSED
    elif failure in {"archived", "forgotten"}:
        parent.lifecycle = LifecycleState(failure)
    elif failure == "expired":
        parent.temporal.t_invalid = SPAN_START
    elif failure == "invalid_span":
        parent.hierarchy.span_end = None
    elif failure == "uncovered_span":
        parent.hierarchy.span_start = SPAN_START + timedelta(minutes=1)
    elif failure == "wrong_backref":
        parent.hierarchy.child_ids = []
        parent.hierarchy.child_scopes = []
    elif failure == "wrong_child_scope":
        parent.hierarchy.child_scopes = [replace(QUERY_SCOPE, session="other")]
    elif failure == "filtered":
        child.user_metadata["visible"] = True
        request.query.recheck_filters = normalize([
            FilterClause("user_metadata.visible", FilterOp.EQ, True),
        ])
    elif failure == "event_window":
        request.query.time_from = SPAN_START
        parent.temporal.t_event = SPAN_START - timedelta(seconds=1)
    store = read_spy([] if failure == "missing" else [parent])
    if failure == "read_error":
        store.get.side_effect = RuntimeError("sensitive backend detail")
    elif failure == "wrong_payload_scope":
        store.get.return_value = [replace(parent, scope=replace(QUERY_SCOPE, session="wrong"))]
    outcome = rollup_candidates(store, request)
    assert outcome.candidates == []
    assert len(outcome.issues) == 1
    assert "sensitive" not in outcome.issues[0]


def test_history_and_include_archived_apply_to_every_ancestor() -> None:
    parent = tree_unit("parent")
    child = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    link(parent, [child])
    parent.lifecycle = LifecycleState.ARCHIVED
    parent.temporal.t_valid = SPAN_START - timedelta(days=1)
    parent.temporal.t_invalid = SPAN_START + timedelta(days=1)
    request = rollup_input([ScoredMemoryUnit(child, 0.8)])
    request.query.as_of = SPAN_START
    store = read_spy([parent])
    outcome = rollup_candidates(store, request)
    assert [item.unit_id for item in outcome.candidates] == [parent.id]
    parent.temporal.t_invalid = None
    request.query.as_of = None
    request.query.include_archived = True
    assert rollup_candidates(store, request).issues == []


def test_nearest_role_stops_before_higher_same_role_or_hidden_intermediate() -> None:
    root = tree_unit("root", HierarchyRole.SCENE)
    nearest = tree_unit("nearest", HierarchyRole.SCENE)
    middle = tree_unit("middle")
    leaf = tree_unit("leaf", HierarchyRole.SNAPSHOT)
    link(root, [nearest])
    link(nearest, [middle])
    link(middle, [leaf])
    store = read_spy([root, nearest, middle])
    request = rollup_input([ScoredMemoryUnit(leaf, 0.8)], target_role=HierarchyRole.SCENE)
    outcome = rollup_candidates(store, request)
    assert [item.unit_id for item in outcome.candidates] == [nearest.id]
    assert store.get.call_count == 2
    middle.lifecycle = LifecycleState.FORGOTTEN
    outcome = rollup_candidates(store, request)
    assert outcome.candidates == []
    assert outcome.issues == ["visibility_excluded"]


@pytest.mark.parametrize("limit", ["cycle", "hop_limit", "node_limit"])
def test_walk_stops_at_cycle_or_resource_limit(limit) -> None:
    first = tree_unit("first", HierarchyRole.SNAPSHOT)
    second = tree_unit("second", HierarchyRole.SNAPSHOT)
    third = tree_unit("third", HierarchyRole.SNAPSHOT)
    link(second, [first])
    link(third, [second])
    link(first, [third])
    request = rollup_input([ScoredMemoryUnit(first, 0.8)])
    if limit == "hop_limit":
        request = replace(request, max_hops=1)
    elif limit == "node_limit":
        request = replace(request, node_limit=1)
    store = read_spy([first, second, third])
    outcome = rollup_candidates(store, request)
    assert outcome.candidates == []
    assert outcome.issues == [limit]
    assert store.get.call_count <= 2


def test_same_id_parents_in_distinct_scopes_remain_distinct() -> None:
    parents = [tree_unit("parent"), tree_unit("parent")]
    leaves = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(2)]
    for parent_index, parent in enumerate(parents):
        parent.scope = replace(QUERY_SCOPE, session=str(parent_index))
        link(parent, [leaves[parent_index]])
    outcome = rollup_candidates(read_spy(parents), rollup_input([
        ScoredMemoryUnit(leaf, 0.8) for leaf in leaves
    ]))
    assert len(outcome.candidates) == 2
    assert {item.unit.scope.session for item in outcome.candidates} == {"0", "1"}


def test_bad_branch_does_not_remove_healthy_parent_or_direct_hit() -> None:
    parent = tree_unit("good-parent")
    good = tree_unit("good-leaf", HierarchyRole.SNAPSHOT)
    bad = tree_unit("bad-leaf", HierarchyRole.SNAPSHOT)
    direct = tree_unit("direct")
    link(parent, [good])
    bad.hierarchy.parent_id = "missing"
    candidates = [ScoredMemoryUnit(unit, 0.8) for unit in [good, bad, direct]]
    outcome = rollup_candidates(read_spy([parent]), rollup_input(candidates))
    assert {item.unit_id for item in outcome.candidates} == {parent.id, direct.id}
    assert outcome.issues == ["missing_parent"]
