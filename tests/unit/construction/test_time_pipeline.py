# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小 TIME 分组、结构父生成、深拷贝和阶段配置边界。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
from uuid import UUID

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    ContentLayers,
    HierarchyKind,
    HierarchyRole,
    LifecycleState,
    MemoryTier,
    Segment,
    Temporal,
    validate_tree,
)
from jiuwen_memory.construction.hierarchy_composer_impl.time_pipeline import (
    DEFAULT_GAP_SECONDS,
    TimeSpanMerger,
    TimeSpanMergerOptions,
    build_time_span_parent,
    run_time_pipeline,
)
from tests.unit.construction.time_pipeline_fixtures import (
    TREE_HOME_SCOPE,
    at,
    group_ids,
    make_leaf,
)

pytestmark = pytest.mark.unit


def test_options_default_values_and_immutable_contract() -> None:
    options = TimeSpanMergerOptions()
    assert options.gap_seconds == DEFAULT_GAP_SECONDS == 7200
    assert options.summary_max_leaves == 20
    assert options.summary_max_chars_per_leaf == 60
    assert options.boundary_metadata_keys == ()
    assert options.carry_metadata_keys == ()
    with pytest.raises(FrozenInstanceError):
        options.gap_seconds = 60


def test_stage_options_parse_inner_mapping() -> None:
    options = TimeSpanMergerOptions.from_stage_options(
        {
            "gap_seconds": " 1800 ",
            "boundary_metadata_keys": "device_id, app, ,device_id",
            "carry_metadata_keys": "title",
            "summary_max_leaves": "2",
            "summary_max_chars_per_leaf": "8",
            "summary_mode": "structural",
        }
    )
    assert options == TimeSpanMergerOptions(1800, ("device_id", "app"), ("title",), 2, 8)
    assert TimeSpanMergerOptions.from_stage_options({}) == TimeSpanMergerOptions()


@pytest.mark.parametrize(
    "field_name", ["gap_seconds", "summary_max_leaves", "summary_max_chars_per_leaf"]
)
@pytest.mark.parametrize("raw_value", ["0", "-1", "True", "False", "1.2", "nan", "inf", "bad"])
def test_stage_options_reject_invalid_positive_integers(field_name, raw_value) -> None:
    with pytest.raises(ValidationError, match="正整数"):
        TimeSpanMergerOptions.from_stage_options({field_name: raw_value})


@pytest.mark.parametrize(
    "stage_options",
    [
        {"summary_mode": "semantic"},
        {"summary_mode": "unknown"},
        {"summary_mode": ""},
        {"end_signal_metadata_keys": ["done"]},
        {"similarity_threshold": "0.7"},
        {"max_duration_seconds": "60"},
        {"settle_seconds": "60"},
        {"settle_at": "2026-09-10"},
        {"SceneSegmenter": {}},
        {"EventBuilder": {}},
        {"TimeSpanMerger": {}},
        {"gap_second": "60"},
    ],
)
def test_stage_options_reject_unimplemented_features(stage_options) -> None:
    with pytest.raises(ValidationError):
        TimeSpanMergerOptions.from_stage_options(stage_options)


@pytest.mark.parametrize(
    "invalid_options",
    [
        {"gap_seconds": True},
        {"gap_seconds": 0},
        {"gap_seconds": 1.5},
        {"summary_max_leaves": -1},
        {"summary_max_chars_per_leaf": "60"},
        {"boundary_metadata_keys": ["device_id"]},
        {"carry_metadata_keys": ("",)},
        {"carry_metadata_keys": (1,)},
    ],
)
def test_direct_options_construction_is_validated(invalid_options) -> None:
    with pytest.raises(ValidationError):
        TimeSpanMergerOptions(**invalid_options)


def test_empty_input_has_no_groups_or_nodes() -> None:
    assert TimeSpanMerger().merge([]) == []
    assert run_time_pipeline(
        [], tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions()
    ) == ([], [])
    with pytest.raises(ValidationError, match="至少一个"):
        build_time_span_parent([], tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions())


def test_unsorted_input_is_sorted_without_mutation() -> None:
    leaves = [make_leaf("last", 30), make_leaf("first", 0), make_leaf("middle", 10)]
    before = deepcopy(leaves)
    merged = TimeSpanMerger().merge(leaves)
    assert group_ids(merged) == [["first", "middle", "last"]]
    assert leaves == before
    assert merged[0][0] is leaves[1]


def test_equal_start_uses_event_time_then_stable_input_order() -> None:
    leaves = [make_leaf(uid, 0) for uid in ("later", "stable_a", "unknown", "stable_b")]
    leaves[0].temporal.t_event = at(2)
    leaves[1].temporal.t_event = at(1)
    leaves[3].temporal.t_event = at(1)
    assert group_ids(TimeSpanMerger().merge(leaves)) == [
        ["unknown", "stable_a", "stable_b", "later"]
    ]


def test_sort_and_interval_compare_utc_naive_and_aware_times() -> None:
    first = make_leaf("first", 0)
    first.hierarchy.span_start = at(0).replace(tzinfo=None)
    first.hierarchy.span_end = at(0).replace(tzinfo=None)
    later = make_leaf("later", 120)
    later.hierarchy.span_start = at(120).astimezone(timezone(timedelta(hours=8)))
    later.hierarchy.span_end = later.hierarchy.span_start
    assert group_ids(TimeSpanMerger().merge([later, first])) == [["first", "later"]]


def test_equal_start_event_tie_breaker_uses_utc() -> None:
    earlier_event = make_leaf("earlier", 0)
    later_event = make_leaf("later", 0)
    earlier_event.temporal.t_event = at(1).astimezone(timezone(timedelta(hours=8)))
    later_event.temporal.t_event = at(2).replace(tzinfo=None)
    assert group_ids(TimeSpanMerger().merge([later_event, earlier_event])) == [["earlier", "later"]]


@pytest.mark.parametrize("delta_microseconds, expected_groups", [(-1, 1), (0, 1), (1, 2)])
def test_gap_threshold_preserves_microsecond_boundary(delta_microseconds, expected_groups) -> None:
    first = make_leaf("first", 0)
    later = make_leaf("later", 120)
    later.hierarchy.span_start += timedelta(microseconds=delta_microseconds)
    later.hierarchy.span_end = later.hierarchy.span_start
    assert len(TimeSpanMerger().merge([first, later])) == expected_groups


def test_gap_uses_previous_end_not_previous_start_or_event_time() -> None:
    first = make_leaf("first", 0, duration=60)
    later = make_leaf("later", 180)
    first.temporal.t_event = at(-10000)
    later.temporal.t_event = at(10000)
    assert group_ids(TimeSpanMerger().merge([first, later])) == [["first", "later"]]


def test_gap_is_adjacent_not_total_group_duration() -> None:
    leaves = [make_leaf(str(index), index * 20) for index in range(6)]
    merger = TimeSpanMerger(TimeSpanMergerOptions(gap_seconds=1800))
    assert group_ids(merger.merge(leaves)) == [["0", "1", "2", "3", "4", "5"]]


def test_overlap_does_not_split_but_gap_uses_immediately_previous_leaf() -> None:
    leaves = [make_leaf("long", 0, duration=300), make_leaf("short", 10), make_leaf("later", 150)]
    assert group_ids(TimeSpanMerger().merge(leaves)) == [["long", "short"], ["later"]]


def test_session_change_splits_even_at_same_time() -> None:
    first = make_leaf("first", 0)
    other_session = make_leaf("other", 0, unit_scope=replace(first.scope, session="session2"))
    assert group_ids(TimeSpanMerger().merge([first, other_session])) == [["first"], ["other"]]


def test_context_changes_only_split_for_configured_system_keys() -> None:
    leaves = [
        make_leaf("first", 0, metadata={"device_id": "desktop"}),
        make_leaf("second", 1, metadata={"device_id": "phone"}),
        make_leaf("third", 2),
    ]
    assert group_ids(TimeSpanMerger().merge(leaves)) == [["first", "second", "third"]]
    merger = TimeSpanMerger(TimeSpanMergerOptions(boundary_metadata_keys=("device_id",)))
    assert group_ids(merger.merge(leaves)) == [["first"], ["second"], ["third"]]


def test_user_metadata_and_content_do_not_control_grouping() -> None:
    first = make_leaf("first", 0)
    second = make_leaf("second", 1)
    first.user_metadata["device_id"] = "desktop"
    second.user_metadata["device_id"] = "phone"
    first.segments = [Segment(content="编写代码")]
    second.segments = [Segment(content="准备晚餐")]
    merger = TimeSpanMerger(TimeSpanMergerOptions(boundary_metadata_keys=("device_id",)))
    assert group_ids(merger.merge([first, second])) == [["first", "second"]]


@pytest.mark.parametrize("field_name", ["span_start", "span_end"])
@pytest.mark.parametrize("invalid_value", [None, "2026-09-10"])
def test_missing_or_invalid_span_is_rejected(field_name, invalid_value) -> None:
    leaf = make_leaf("invalid", 0)
    setattr(leaf.hierarchy, field_name, invalid_value)
    with pytest.raises(ValidationError, match="datetime"):
        TimeSpanMerger().merge([leaf])


def test_reversed_span_and_invalid_event_time_are_rejected() -> None:
    leaf = make_leaf("invalid", 1)
    leaf.hierarchy.span_end = at(0)
    with pytest.raises(ValidationError, match="不得晚于"):
        TimeSpanMerger().merge([leaf])
    leaf.hierarchy.span_end = at(1)
    leaf.temporal.t_event = "yesterday"
    with pytest.raises(ValidationError, match="datetime"):
        TimeSpanMerger().merge([leaf])


@pytest.mark.parametrize(
    "role", [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT]
)
def test_non_snapshot_roles_are_rejected(role) -> None:
    leaf = make_leaf("invalid", 0)
    leaf.hierarchy.role = role
    with pytest.raises(ValidationError, match="snapshot"):
        TimeSpanMerger().merge([leaf])


def test_other_kind_and_snapshot_with_children_are_rejected() -> None:
    leaf = make_leaf("invalid", 0)
    leaf.hierarchy.kind = HierarchyKind.TOPIC
    with pytest.raises(ValidationError, match="TIME snapshot"):
        TimeSpanMerger().merge([leaf])
    leaf.hierarchy.kind = HierarchyKind.TIME
    leaf.hierarchy.child_ids = ["other"]
    with pytest.raises(ValidationError, match="没有子节点"):
        TimeSpanMerger().merge([leaf])


def test_duplicate_identity_rejected_but_same_id_in_other_scope_allowed() -> None:
    first = make_leaf("same", 0)
    with pytest.raises(ValidationError, match="重复节点"):
        TimeSpanMerger().merge([first, deepcopy(first)])
    other = make_leaf("same", 0, unit_scope=replace(first.scope, session="other"))
    assert group_ids(TimeSpanMerger().merge([first, other])) == [["same"], ["same"]]


def test_parent_uses_union_span_new_id_and_structural_body() -> None:
    children = [make_leaf("first", 0, duration=120), make_leaf("second", 60)]
    parent = build_time_span_parent(
        children, tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions()
    )
    assert str(UUID(parent.id)) == parent.id
    assert parent.id not in {child.id for child in children}
    assert parent.scope == TREE_HOME_SCOPE
    assert parent.tier is MemoryTier.EPISODIC
    assert parent.hierarchy.role is HierarchyRole.TIME_SPAN
    assert parent.hierarchy.child_ids == ["first", "second"]
    assert parent.hierarchy.child_scopes == [child.scope for child in children]
    assert parent.hierarchy.span_start == at(0)
    assert parent.hierarchy.span_end == at(120)
    assert parent.segments[0].content == f"{at(0).isoformat()} ~ {at(120).isoformat()}（2 条记录）"
    assert parent.segments[1].content == "- first 的内容\n- second 的内容"
    assert parent.provenance == []
    assert parent.temporal.t_event is None
    assert parent.temporal.t_ingest is not None
    assert all(child.hierarchy.parent_id == "" for child in children)


def test_parent_excerpt_limits_preserve_all_leaf_segments_and_omission_count() -> None:
    children = [make_leaf(str(index), index) for index in range(3)]
    children[0].segments = [Segment(content=" - first \n"), Segment(content="second  ")]
    children[1].segments = [Segment(content="123456789")]
    before = deepcopy(children)
    options = TimeSpanMergerOptions(summary_max_leaves=2, summary_max_chars_per_leaf=8)
    parent = build_time_span_parent(children, tree_home_scope=TREE_HOME_SCOPE, options=options)
    assert parent.segments[1].content == "- first se\n- 12345678\n- …另有 1 条"
    assert parent.hierarchy.child_ids == ["0", "1", "2"]
    assert children == before


def test_parent_shared_metadata_and_user_intersection_are_independent() -> None:
    shared = {"device": "desktop", "infer": "true", "app": "editor", "number": 1}
    children = [make_leaf("first", 0, metadata=shared), make_leaf("second", 1, metadata=shared)]
    children[0].system_metadata["title"] = "one"
    children[1].system_metadata["title"] = "two"
    children[0].user_metadata = {"labels": ["shared"], "local": "first"}
    children[1].user_metadata = {"labels": ["shared"], "local": "second"}
    children[0].entities = ["Python", "TIME"]
    children[1].entities = ["TIME", "Memory"]
    options = TimeSpanMergerOptions(
        boundary_metadata_keys=("device",), carry_metadata_keys=("app", "title", "number", "infer")
    )
    extra = {"profile": "minimal", "device": "wrong", "middle": "true"}
    parent = build_time_span_parent(
        children, tree_home_scope=TREE_HOME_SCOPE, options=options, extra_metadata=extra
    )
    assert parent.system_metadata == {"profile": "minimal", "device": "desktop", "app": "editor"}
    assert parent.user_metadata == {"labels": ["shared"]}
    assert parent.entities == ["Python", "TIME", "Memory"]
    parent.user_metadata["labels"].append("parent-only")
    assert children[0].user_metadata["labels"] == ["shared"]
    assert extra == {"profile": "minimal", "device": "wrong", "middle": "true"}


def test_pipeline_backlinks_are_valid_and_only_change_hierarchy() -> None:
    leaves = [make_leaf("late", 180), make_leaf("first", 0), make_leaf("middle", 1)]
    leaves[0].layers = ContentLayers(l0="summary", l1="detail")
    leaves[0].tier = MemoryTier.SEMANTIC
    leaves[0].source_ref = "payload-id"
    leaves[0].provenance = ["source-id"]
    leaves[0].lifecycle = LifecycleState.ARCHIVED
    leaves[0].temporal = Temporal(t_ingest=at(500), t_event=at(-1), t_message=at(10))
    before = deepcopy(leaves)
    parents, attached = run_time_pipeline(
        leaves, tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions()
    )
    validate_tree(parents + attached)
    assert len(parents) == 2
    assert parents[0].hierarchy.child_ids == ["first", "middle"]
    assert parents[1].hierarchy.child_ids == ["late"]
    assert [child.id for child in attached] == ["late", "first", "middle"]
    assert leaves == before
    for original, output in zip(leaves, attached):
        assert output is not original
        assert replace(output, hierarchy=deepcopy(original.hierarchy)) == original
        assert output.hierarchy.parent_scope == TREE_HOME_SCOPE
    assert attached[0].hierarchy.parent_id == parents[1].id
    assert attached[1].hierarchy.parent_id == parents[0].id


def test_pipeline_detaches_all_mutable_scope_and_metadata_inputs() -> None:
    source = make_leaf("leaf", 0)
    source.user_metadata = {"labels": ["original"]}
    home_scope = deepcopy(TREE_HOME_SCOPE)
    before = deepcopy(source)
    parents, attached = run_time_pipeline(
        [source], tree_home_scope=home_scope, options=TimeSpanMergerOptions()
    )
    parents[0].scope.user = "mutated-parent"
    parents[0].hierarchy.child_scopes[0].session = "mutated-child-ref"
    attached[0].scope.session = "mutated-child"
    attached[0].hierarchy.parent_scope.user = "mutated-parent-ref"
    attached[0].user_metadata["labels"].append("mutated")
    assert source == before
    assert home_scope == TREE_HOME_SCOPE


def test_pipeline_failure_does_not_modify_previous_valid_inputs() -> None:
    valid = make_leaf("valid", 0)
    invalid = make_leaf("invalid", 1)
    invalid.hierarchy.span_end = None
    leaves = [valid, invalid]
    before = deepcopy(leaves)
    with pytest.raises(ValidationError):
        run_time_pipeline(leaves, tree_home_scope=TREE_HOME_SCOPE, options=TimeSpanMergerOptions())
    assert leaves == before
