"""展开顺序、范围、真源校验与坏分支的确定性测试。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    Scope,
)
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from tests.unit.retrieval.expansion_fixtures import Collector, expand_request, link, make_tree
from tests.unit.retrieval.hierarchy_query_fixtures import QUERY_SCOPE, SPAN_START, tree_unit

pytestmark = pytest.mark.unit


def test_expander_reads_session_children_in_declared_order() -> None:
    tree = make_tree()
    collector = Collector()
    original = deepcopy(tree.root)
    result = tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert [unit.id for unit in collector.selected] == ["first", "second"]
    assert collector.depths == [1, 1]
    assert result.complete and not result.truncated
    assert result.actual_depth == 1
    assert tree.root == original


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_breadth_first_depth_limit_does_not_count_as_truncation(depth) -> None:
    tree = make_tree()
    tree.children[0].hierarchy.role = HierarchyRole.TIME_SPAN
    grandchild = tree_unit("grandchild", HierarchyRole.SNAPSHOT)
    grandchild.scope = tree.children[0].scope
    link(tree.children[0], [grandchild])
    tree.save(tree.children)
    tree.base.domain.add(grandchild.scope, [grandchild])
    collector = Collector()
    result = tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, collector, depth=depth))
    expected = ["first", "second"] if depth == 1 else ["first", "second", "grandchild"]
    assert [unit.id for unit in collector.selected] == expected
    assert result.actual_depth == min(depth, 2)
    assert result.complete and not result.truncated


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent"])
def test_scope_is_checked_before_any_foreign_point_read(dimension) -> None:
    tree = make_tree()
    foreign = replace(tree.children[0].scope, **{dimension: "foreign"})
    tree.root.hierarchy.child_scopes[0] = foreign
    store = Mock(wraps=tree.base.domain)
    collector = Collector()
    result = DefaultExpander(store).expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert [unit.id for unit in collector.selected] == ["second"]
    assert "scope_excluded" in [issue.code for issue in result.issues]
    assert all(call.args[0] != foreign for call in store.get.call_args_list)


def test_requested_session_is_not_widened_by_a_child_reference() -> None:
    tree = make_tree()
    scope = replace(QUERY_SCOPE, session="allowed")
    tree.root.scope = scope
    collector = Collector()
    result = tree.expander.expand(scope, expand_request(tree.root, collector))
    assert collector.selected == []
    assert not result.complete


@pytest.mark.parametrize("problem", ["missing", "kind", "status", "parent", "parent_scope", "span"])
def test_bad_child_does_not_hide_healthy_sibling(problem) -> None:
    tree = make_tree()
    child = tree.children[0]
    if problem == "missing":
        tree.root.hierarchy.child_ids[0] = "missing-id"
    elif problem == "kind":
        child.hierarchy.kind = HierarchyKind.TOPIC
    elif problem == "status":
        child.hierarchy.status = HierarchyStatus.DISMISSED
    elif problem == "parent":
        child.hierarchy.parent_id = "wrong-parent"
    elif problem == "parent_scope":
        child.hierarchy.parent_scope = replace(tree.root.scope, session="wrong")
    else:
        child.hierarchy.span_start -= timedelta(days=1)
    tree.save([child])
    collector = Collector()
    result = tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert [unit.id for unit in collector.selected] == ["second"]
    assert not result.complete and not result.truncated


def test_wrong_scope_or_id_returned_by_backend_is_not_accepted() -> None:
    tree = make_tree()
    forged = deepcopy(tree.children[0])
    forged.scope = replace(forged.scope, org="foreign")
    store = Mock()
    store.get.return_value = [forged]
    collector = Collector()
    result = DefaultExpander(store).expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert collector.selected == []
    assert "missing_child" in [issue.code for issue in result.issues]


def test_cycle_stops_without_suppressing_siblings() -> None:
    tree = make_tree()
    tree.children[0].hierarchy.child_ids = [tree.root.id]
    tree.children[0].hierarchy.child_scopes = [tree.root.scope]
    tree.save(tree.children)
    collector = Collector()
    result = tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, collector, depth=10))
    assert [unit.id for unit in collector.selected] == ["first", "second"]
    assert "cycle" in [issue.code for issue in result.issues]


def test_equal_ids_in_different_sessions_are_distinct_nodes() -> None:
    tree = make_tree()
    tree.children[1].id = tree.children[0].id
    link(tree.root, tree.children)
    tree.base.domain.add(tree.children[1].scope, [tree.children[1]])
    collector = Collector()
    result = tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert len(collector.selected) == 2
    assert collector.selected[0].scope != collector.selected[1].scope
    assert result.complete


def test_valid_time_and_business_filters_are_inherited() -> None:
    tree = make_tree()
    child = tree.children[0]
    child.lifecycle = LifecycleState.SUPERSEDED
    child.temporal.t_valid = SPAN_START - timedelta(days=1)
    child.temporal.t_invalid = SPAN_START + timedelta(days=1)
    child.user_metadata["visible"] = True
    tree.save(tree.children)
    collector = Collector()
    request = expand_request(tree.root, collector)
    request.query.as_of = SPAN_START
    request.query.recheck_filters = FilterClause("user_metadata.visible", FilterOp.EQ, True)
    result = tree.expander.expand(QUERY_SCOPE, request)
    assert [unit.id for unit in collector.selected] == ["first"]
    assert not result.complete


def test_node_limit_bounds_point_reads_and_reports_truncation() -> None:
    tree = make_tree()
    store = Mock(wraps=tree.base.domain)
    collector = Collector()
    result = DefaultExpander(store).expand(
        QUERY_SCOPE, expand_request(tree.root, collector, node_limit=1),
    )
    assert [unit.id for unit in collector.selected] == ["first"]
    assert result.visited_count == 1
    assert result.truncated and not result.complete
    assert sum(len(call.args[1]) for call in store.get.call_args_list) == 1


def test_foreign_root_is_rejected_without_reading() -> None:
    tree = make_tree()
    with pytest.raises(NotFoundError):
        tree.expander.expand(Scope(org="foreign"), expand_request(tree.root, Collector()))


@pytest.mark.parametrize("depth", [0, -1, True, "1"])
def test_internal_expander_rejects_invalid_depth(depth) -> None:
    tree = make_tree()
    with pytest.raises(ValidationError):
        tree.expander.expand(QUERY_SCOPE, expand_request(tree.root, Collector(), depth=depth))


@pytest.mark.parametrize("lifecycle", list(LifecycleState))
@pytest.mark.parametrize("mode", ["current", "archived", "history"])
def test_child_lifecycle_reuses_current_and_historical_visibility(lifecycle, mode) -> None:
    tree = make_tree()
    tree.children[0].lifecycle = lifecycle
    tree.save(tree.children)
    collector = Collector()
    request = expand_request(tree.root, collector)
    request.query.include_archived = mode == "archived"
    request.query.as_of = SPAN_START if mode == "history" else None
    tree.expander.expand(QUERY_SCOPE, request)
    expected = lifecycle is LifecycleState.ACTIVE
    if mode == "history":
        expected = lifecycle is not LifecycleState.FORGOTTEN
    elif mode == "archived":
        expected = lifecycle in (LifecycleState.ACTIVE, LifecycleState.ARCHIVED)
    assert ("first" in [unit.id for unit in collector.selected]) == expected


@pytest.mark.parametrize("axis", ["event", "span"])
def test_child_time_conditions_are_not_removed_with_parent_role(axis) -> None:
    tree = make_tree()
    tree.children[0].temporal.t_event = SPAN_START
    tree.children[0].hierarchy.span_end = SPAN_START
    tree.save(tree.children)
    collector = Collector()
    request = expand_request(tree.root, collector)
    beginning = SPAN_START + timedelta(minutes=1)
    if axis == "event":
        request.query.time_from = beginning
    else:
        request.query.span_start = beginning
    result = tree.expander.expand(QUERY_SCOPE, request)
    assert [unit.id for unit in collector.selected] == ["second"]
    assert "visibility_excluded" in [issue.code for issue in result.issues]


def test_batch_not_found_falls_back_without_losing_healthy_siblings() -> None:
    tree = make_tree()
    tree.children[1].scope = tree.children[0].scope
    link(tree.root, tree.children)
    tree.base.domain.add(tree.children[1].scope, [tree.children[1]])
    store = Mock()
    store.get.side_effect = [NotFoundError("batch"), NotFoundError("missing"), [tree.children[1]]]
    collector = Collector()
    result = DefaultExpander(store).expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert [unit.id for unit in collector.selected] == ["second"]
    assert "missing_child" in [issue.code for issue in result.issues]


def test_backend_failure_diagnostics_do_not_expose_exception_data() -> None:
    tree = make_tree()
    store = Mock()
    store.get.side_effect = RuntimeError("secret payload")
    collector = Collector()
    result = DefaultExpander(store).expand(QUERY_SCOPE, expand_request(tree.root, collector))
    assert collector.selected == []
    assert "read_error" in [issue.code for issue in result.issues]
    assert "secret" not in str(result)
