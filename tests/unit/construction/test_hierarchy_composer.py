# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME Composer 的公开契约、校验零副作用和顺序写入失败报告。"""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    Scope,
    validate_tree,
)
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposerProducer
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.unit.construction.hierarchy_fixtures import (
    ORIGIN,
    RecordingIndexBuilder,
    make_harness,
    make_leaf,
    make_request,
    replacement_request,
)

pytestmark = pytest.mark.unit


def test_build_persists_two_layer_tree_without_mutating_inputs() -> None:
    leaves = [make_leaf("later", 20), make_leaf("earlier", 0)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    original = deepcopy(request)

    result = harness.composer.build(request)

    assert result.complete
    assert not result.repair_required
    assert len(result.created_parent_ids) == 1
    assert request == original
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert parent.hierarchy.child_ids == ["earlier", "later"]
    assert parent.hierarchy.child_scopes == [leaves[1].scope, leaves[0].scope]
    assert parent.hierarchy.span_start == ORIGIN
    assert parent.hierarchy.span_end == ORIGIN + timedelta(minutes=20)
    assert parent.hierarchy.role is HierarchyRole.TIME_SPAN
    assert parent.system_metadata["build_source"] == "manual"
    persisted_leaves = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    for persisted, untouched in zip(persisted_leaves, original.leaves):
        assert persisted.hierarchy.parent_id == parent.id
        assert persisted.hierarchy.parent_scope == parent.scope
        persisted.hierarchy = deepcopy(untouched.hierarchy)
        assert persisted == untouched, "除 hierarchy 外，原始事实全部字段必须保持不变"
    assert [(call.method, call.mode) for call in harness.builder.calls] == [
        ("build", IndexWriteMode.FORWARD_ONLY),
        ("update", IndexWriteMode.FORWARD_ONLY),
        ("build", IndexWriteMode.RETRIEVAL_ONLY),
        ("update", IndexWriteMode.RETRIEVAL_ONLY),
    ]
    assert harness.manager.fulltext().get(parent.scope, [parent.id])


def test_two_replacements_archive_old_parents_without_duplicate_insert() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    first = harness.composer.build(request)
    old_ids = first.created_parent_ids
    observed_ids = set(old_ids)

    for _ in range(2):
        replacing = replacement_request(harness, request, old_ids)
        original_replacement = deepcopy(replacing)
        harness.builder.calls.clear()
        result = harness.composer.replace_in_span(replacing)

        assert result.complete, "真实内存 KV/全文 insert 会拒绝重复键，不能重复交付新父本体"
        assert replacing == original_replacement
        assert not observed_ids.intersection(result.created_parent_ids)
        observed_ids.update(result.created_parent_ids)
        assert result.replaced_parent_ids == old_ids
        retired = harness.read(request.options.tree_home_scope, old_ids[0])
        assert retired.lifecycle is LifecycleState.ARCHIVED
        assert not retired.hierarchy.child_ids
        assert not retired.hierarchy.child_scopes
        assert not retired.hierarchy.parent_id
        assert retired.hierarchy.parent_scope is None
        assert harness.manager.fulltext().get(retired.scope, old_ids) == []
        current_parent = harness.read(retired.scope, result.created_parent_ids[0])
        current_children = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
        validate_tree([current_parent, *current_children])
        assert [(call.method, call.mode) for call in harness.builder.calls] == [
            ("build", IndexWriteMode.FORWARD_ONLY),
            ("update", IndexWriteMode.FORWARD_ONLY),
            ("update", IndexWriteMode.FORWARD_ONLY),
            ("remove", IndexRemoveMode.SOFT),
            ("build", IndexWriteMode.RETRIEVAL_ONLY),
            ("update", IndexWriteMode.RETRIEVAL_ONLY),
        ]
        old_ids = result.created_parent_ids


@pytest.mark.parametrize(
    ("fail_at", "issue"),
    [
        (1, "parent_write_failed"),
        (2, "child_edge_write_failed"),
        (3, "old_parent_retire_failed"),
        (4, "index_remove_failed"),
        (5, "index_build_failed"),
        (6, "index_update_failed"),
    ],
)
def test_each_write_failure_reports_incomplete_without_mutating_inputs(fail_at, issue) -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    initial = harness.composer.build(request)
    replacing = replacement_request(harness, request, initial.created_parent_ids)
    original = deepcopy(replacing)
    harness.builder.calls.clear()
    harness.builder.fail_at = fail_at

    result = harness.composer.replace_in_span(replacing)

    assert not result.complete
    assert {repair.issue for repair in result.repair_required} == {issue}
    failed_call = harness.builder.calls[fail_at - 1]
    assert [repair.unit_id for repair in result.repair_required] == [
        failed_unit.id for failed_unit in failed_call.units
    ]
    assert replacing == original
    assert len(harness.builder.calls) == (fail_at if fail_at <= 3 else 6)
    persisted_leaf = harness.read(leaves[0].scope, leaves[0].id)
    if fail_at <= 2:
        assert persisted_leaf.hierarchy.parent_id == initial.created_parent_ids[0]
    else:
        assert persisted_leaf.hierarchy.parent_id == result.created_parent_ids[0]
        assert harness.read(replacing.options.tree_home_scope, persisted_leaf.hierarchy.parent_id)


@pytest.mark.parametrize(
    "invalid_case",
    [
        "empty_leaves", "duplicate_leaf", "blank_id", "wrong_kind", "wrong_role",
        "archived_leaf", "dismissed_leaf", "invalid_span", "missing_span", "bad_span_type",
        "both_leaf_spans_missing",
        "children_on_snapshot", "dangling_parent_scope", "invalid_scope", "unsupported_chain",
        "reverse_request_span", "unpaired_request_span", "bad_metadata", "outside_span",
        "cross_org_home", "cross_space_home", "cross_user_home", "cross_agent_home",
        "existing_parent_reference",
    ],
)
def test_invalid_build_is_zero_write_and_input_preserving(invalid_case) -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    selected = request.leaves[0]
    changes = {
        "blank_id": (selected, "id", ""),
        "wrong_kind": (selected.hierarchy, "kind", HierarchyKind.TOPIC),
        "wrong_role": (selected.hierarchy, "role", HierarchyRole.TIME_SPAN),
        "archived_leaf": (selected, "lifecycle", LifecycleState.ARCHIVED),
        "dismissed_leaf": (selected.hierarchy, "status", HierarchyStatus.DISMISSED),
        "invalid_span": (selected.hierarchy, "span_end", ORIGIN - timedelta(seconds=1)),
        "missing_span": (selected.hierarchy, "span_end", None),
        "bad_span_type": (selected.hierarchy, "span_end", "not-a-datetime"),
        "children_on_snapshot": (selected.hierarchy, "child_ids", ["other"]),
        "dangling_parent_scope": (selected.hierarchy, "parent_scope", Scope()),
        "invalid_scope": (selected.scope, "org", 1),
        "unsupported_chain": (request.options, "parent_roles", [HierarchyRole.SCENE]),
        "bad_metadata": (request.options, "metadata", {"build_source": 1}),
        "cross_org_home": (request.options.tree_home_scope, "org", "other"),
        "cross_space_home": (request.options.tree_home_scope, "space", "other"),
        "cross_user_home": (request.options.tree_home_scope, "user", "other"),
        "cross_agent_home": (request.options.tree_home_scope, "agent", "other"),
        "existing_parent_reference": (selected.hierarchy, "parent_id", "unknown"),
    }
    if invalid_case in changes:
        target, attribute, invalid_value = changes[invalid_case]
        setattr(target, attribute, invalid_value)
    elif invalid_case == "empty_leaves":
        request.leaves = []
    elif invalid_case == "duplicate_leaf":
        request.leaves.append(deepcopy(selected))
    elif invalid_case == "both_leaf_spans_missing":
        selected.hierarchy.span_start = selected.hierarchy.span_end = None
    else:
        request.options.span_start = ORIGIN + timedelta(hours=2)
        request.options.span_end = ORIGIN + timedelta(hours=3)
        if invalid_case == "reverse_request_span":
            request.options.span_end = ORIGIN
        elif invalid_case == "unpaired_request_span":
            request.options.span_end = None
    original = deepcopy(request)

    with pytest.raises(ValidationError):
        harness.composer.build(request)

    assert harness.builder.calls == []
    assert request == original


@pytest.mark.parametrize(
    "invalid_case",
    ["missing_bounds", "missing_child", "unknown_parent", "disjoint_parent", "wrong_home",
     "duplicate_parent", "archived_parent", "broken_bilateral", "build_with_old_parent",
     "missing_parent_span", "partial_parent_span", "bad_parent_span_type",
     "missing_leaf_span", "partial_leaf_span", "bad_leaf_span_type"],
)
def test_invalid_replacement_is_zero_write_and_input_preserving(invalid_case) -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    initial = harness.composer.build(request)
    replacing = replacement_request(harness, request, initial.created_parent_ids)
    if invalid_case == "missing_bounds":
        replacing.options.span_start = replacing.options.span_end = None
    elif invalid_case == "missing_child":
        replacing.leaves.pop()
    elif invalid_case == "unknown_parent":
        replacing.existing_parents = []
    elif invalid_case == "disjoint_parent":
        replacing.options.span_start += timedelta(days=1)
        replacing.options.span_end += timedelta(days=1)
    elif invalid_case == "wrong_home":
        replacing.options.tree_home_scope.session = "another-session"
    elif invalid_case == "duplicate_parent":
        replacing.existing_parents.append(deepcopy(replacing.existing_parents[0]))
    elif invalid_case == "archived_parent":
        replacing.existing_parents[0].lifecycle = LifecycleState.ARCHIVED
    elif invalid_case == "broken_bilateral":
        replacing.leaves[0].hierarchy.parent_id = "unknown"
    elif invalid_case.endswith("parent_span") or invalid_case == "bad_parent_span_type":
        old_ref = replacing.existing_parents[0].hierarchy
        old_ref.span_end = "bad" if invalid_case == "bad_parent_span_type" else None
        if invalid_case == "missing_parent_span":
            old_ref.span_start = None
    elif invalid_case.endswith("leaf_span") or invalid_case == "bad_leaf_span_type":
        leaf_ref = replacing.leaves[0].hierarchy
        leaf_ref.span_end = "bad" if invalid_case == "bad_leaf_span_type" else None
        if invalid_case == "missing_leaf_span":
            leaf_ref.span_start = None
    original = deepcopy(replacing)
    harness.builder.calls.clear()
    action = harness.composer.build if invalid_case == "build_with_old_parent" else (
        harness.composer.replace_in_span
    )

    with pytest.raises(ValidationError):
        action(replacing)

    assert harness.builder.calls == []
    assert replacing == original


def test_replace_includes_complete_old_children_outside_requested_subspan() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves)
    request = make_request(leaves)
    initial = harness.composer.build(request)
    replacing = replacement_request(harness, request, initial.created_parent_ids)
    replacing.options.span_start = ORIGIN + timedelta(minutes=10)
    replacing.options.span_end = ORIGIN + timedelta(minutes=10)

    result = harness.composer.replace_in_span(replacing)

    assert result.complete
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert parent.hierarchy.child_ids == ["a", "b"]
    assert parent.hierarchy.span_start == ORIGIN
    assert parent.hierarchy.span_end == ORIGIN + timedelta(minutes=20)


def test_same_ids_in_distinct_sessions_keep_complete_identity() -> None:
    first_leaf = make_leaf("same")
    second_scope = deepcopy(first_leaf.scope)
    second_scope.session = "s2"
    second_leaf = make_leaf("same", 10, scope=second_scope)
    leaves = [first_leaf, second_leaf]
    harness = make_harness(leaves)
    request = make_request(leaves)

    result = harness.composer.build(request)

    assert result.complete
    assert len(result.created_parent_ids) == 2
    parents = [
        harness.read(request.options.tree_home_scope, uid) for uid in result.created_parent_ids
    ]
    persisted = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    validate_tree([*parents, *persisted])
    assert persisted[0].hierarchy.parent_id != persisted[1].hierarchy.parent_id
    assert parents[0].hierarchy.child_scopes == [first_leaf.scope]
    assert parents[1].hierarchy.child_scopes == [second_leaf.scope]


def test_scope_grouping_cannot_collide_on_slash_joined_coordinates() -> None:
    leaves = [
        make_leaf("same", scope=Scope(org="org", space="space", user="a/b", agent="c")),
        make_leaf("same", 10, scope=Scope(org="org", space="space", user="a", agent="b/c")),
    ]
    builder = RecordingIndexBuilder()
    composer = DefaultHierarchyComposer(builder, allow_cross_user=True)
    request = make_request(leaves, home_scope=Scope(org="org", space="space"))

    result = composer.build(request)

    assert result.complete
    edge_calls = [call for call in builder.calls if call.mode is IndexWriteMode.FORWARD_ONLY]
    assert len(edge_calls) == 3, "两组完整 Scope 的子引用不能因斜杠拼接碰撞而合并"
    assert len(edge_calls[1].units) == len(edge_calls[2].units) == 1


def test_candidate_tree_validation_happens_before_any_write(monkeypatch) -> None:
    duplicated_id = "00000000-0000-0000-0000-000000000001"
    leaves = [make_leaf(duplicated_id)]
    harness = make_harness(leaves)
    request = make_request(leaves, home_scope=leaves[0].scope)
    original = deepcopy(request)
    monkeypatch.setattr("uuid.uuid4", lambda: UUID(duplicated_id))

    with pytest.raises(ValidationError):
        harness.composer.build(request)

    assert harness.builder.calls == []
    assert request == original


def test_replacement_rejects_generated_parent_id_collision_before_writing(monkeypatch) -> None:
    leaves = [make_leaf("a")]
    harness = make_harness(leaves)
    request = make_request(leaves)
    initial = harness.composer.build(request)
    replacing = replacement_request(harness, request, initial.created_parent_ids)
    harness.builder.calls.clear()
    monkeypatch.setattr("uuid.uuid4", lambda: UUID(initial.created_parent_ids[0]))

    with pytest.raises(ValidationError, match="新父 id"):
        harness.composer.replace_in_span(replacing)

    assert harness.builder.calls == []


def test_partial_scope_write_failure_never_points_to_missing_parent() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    leaves[1].scope.session = "s2"
    harness = make_harness(leaves)
    request = make_request(leaves)
    original = deepcopy(request)
    harness.builder.fail_at = 3

    result = harness.composer.build(request)

    assert not result.complete
    assert result.updated_child_ids == ["a"]
    assert result.repair_required[0].unit_id == "b"
    assert len(result.created_parent_ids) == 2
    updated_leaf = harness.read(leaves[0].scope, "a")
    untouched_leaf = harness.read(leaves[1].scope, "b")
    assert harness.read(updated_leaf.hierarchy.parent_scope, updated_leaf.hierarchy.parent_id)
    assert untouched_leaf == original.leaves[1]
    assert request == original


def test_explicit_cross_user_opt_in_and_coarser_home_preserve_leaf_scopes() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    leaves[1].scope.user = "bob"
    harness = make_harness(leaves, allow_cross_user=True)
    request = make_request(leaves, home_scope=Scope(org="org", space="space"))

    result = harness.composer.build(request)

    assert result.complete
    parent = harness.read(request.options.tree_home_scope, result.created_parent_ids[0])
    assert parent.hierarchy.child_scopes == [leaf.scope for leaf in leaves]
    current_leaves = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    validate_tree([parent, *current_leaves], allow_cross_user=True)


def test_invalid_direct_profile_is_rejected_during_construction() -> None:
    from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeProfile

    unsupported = HierarchyComposeProfile(
        kind=HierarchyKind.TIME,
        leaf_role=HierarchyRole.SNAPSHOT,
        parent_roles=(HierarchyRole.TIME_SPAN, HierarchyRole.EVENT),
    )
    with pytest.raises(ValidationError, match="parent_roles"):
        DefaultHierarchyComposer(RecordingIndexBuilder(), {HierarchyKind.TIME: unsupported})


def test_profile_threshold_changes_grouping_and_default_factory_is_registered() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    profiles = build_profiles({"time": {
        "parent_roles": ["time_span"],
        "stage_options": {"TimeSpanMerger": {"gap_seconds": 600}},
    }})
    harness = make_harness(leaves, profiles=profiles)

    result = harness.composer.build(make_request(leaves))

    assert result.complete
    assert len(result.created_parent_ids) == 2
    assert "default" in HierarchyComposerProducer.known()
    IndexBuilderProducer.put("composer-test-index", harness.builder)
    try:
        registered = HierarchyComposerProducer.build(
            "default", {"index_builder": "composer-test-index"}, AssemblyContext()
        )
        assert isinstance(registered, DefaultHierarchyComposer)
        assert registered.index_builder is harness.builder
        assert registered.operator_type() is OperatorType.HIERARCHY_COMPOSER
        registered.health()
    finally:
        IndexBuilderProducer.reset_instances()


@pytest.mark.parametrize(
    "raw_profile",
    [
        [], {"topic": {}}, {"time": []}, {"time": {}},
        {"time": {"parent_roles": ["time_span", "event"]}},
        {"time": {"parent_roles": ["time_span"], "leaf_role": "time_span"}},
        {"time": {"parent_roles": ["time_span"], "unknown": True}},
        {"time": {"parent_roles": ["time_span"], "stage_options": []}},
        {"time": {"parent_roles": ["time_span"], "stage_options": {"SceneSegmenter": {}}}},
        {"time": {"parent_roles": ["time_span"], "stage_options": {"TimeSpanMerger": []}}},
        {"time": {"parent_roles": ["time_span"], "stage_options": {
            "TimeSpanMerger": {"summary_mode": "unknown"}}}},
        {"time": {"parent_roles": ["time_span"], "stage_options": {
            "TimeSpanMerger": {"gap_seconds": True}}}},
        {"time": {"parent_roles": ["time_span"], "stage_options": {
            "TimeSpanMerger": {"gap_seconds": None}}}},
    ],
)
def test_profiles_reject_unimplemented_or_invalid_configuration(raw_profile) -> None:
    with pytest.raises(ValidationError):
        build_profiles(raw_profile)


def test_empty_profiles_use_algorithm_defaults_and_direct_policy_type_is_checked() -> None:
    assert build_profiles(None) == {}
    assert build_profiles({}) == {}
    configured = build_profiles({"time": {"parent_roles": "time_span"}})
    assert configured[HierarchyKind.TIME].parent_roles == (HierarchyRole.TIME_SPAN,)
    with pytest.raises(ValidationError, match="allow_cross_user"):
        DefaultHierarchyComposer(RecordingIndexBuilder(), allow_cross_user="false")
