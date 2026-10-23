# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""单层增量契约、尾组与下层边界、直接子摘要分组及内容保真。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.hierarchy_composer_impl.parent_enrichment import (
    HierarchyModelDependencies,
)
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from tests.unit.construction.event_fixtures import event_profiles
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_harness, make_leaf, make_request
from tests.unit.construction.incremental_fixtures import incremental_request, subtree_evidence
from tests.unit.construction.scene_fixtures import RecordingEmbedder, RecordingLLM

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("seconds,created", [(1, 0), (2, 1)])
def test_snapshot_tail_settles_only_strictly_after_gap(seconds, created) -> None:
    leaf = make_leaf("tail")
    harness = make_harness([leaf], profiles=event_profiles())
    request = incremental_request([leaf], now=ORIGIN + timedelta(seconds=seconds))
    original = deepcopy(request)
    result = harness.composer.build(request)
    assert result.complete
    assert len(result.created_parent_ids) == created
    assert result.deferred_child_count == 1 - created
    assert request == original
    if created == 0:
        assert result.pending_before == ORIGIN
        assert not harness.builder.calls


def test_closed_prefix_can_commit_while_last_group_waits() -> None:
    leaves = [make_leaf("old"), make_leaf("tail", 1)]
    harness = make_harness(leaves, profiles=event_profiles())
    result = harness.composer.build(incremental_request(
        leaves, now=ORIGIN + timedelta(minutes=1),
    ))
    assert result.complete
    assert result.updated_child_ids == ["old"]
    assert result.deferred_child_count == 1
    assert result.pending_before == ORIGIN + timedelta(minutes=1)
    assert harness.read(leaves[1].scope, "tail").hierarchy.parent_id == ""


def test_lower_pending_frontier_blocks_even_quiet_upper_group() -> None:
    leaf = make_leaf("evidence")
    harness = make_harness([leaf], profiles=event_profiles())
    first = harness.composer.build(make_request([leaf]))
    home = make_request([leaf]).options.tree_home_scope
    span = harness.read(home, first.created_parent_ids[0])
    request = incremental_request([span], role=HierarchyRole.SCENE,
                                  support=subtree_evidence(harness, [span]))
    request.incremental.ready_before = ORIGIN
    harness.builder.calls.clear()
    result = harness.composer.build(request)
    assert result.complete
    assert result.deferred_child_count == 1
    assert not result.created_parent_ids
    assert not harness.builder.calls
    assert harness.read(home, span.id) == span


def test_overlapping_closed_group_must_not_advance_past_unsettled_input() -> None:
    first = make_leaf("long")
    first.hierarchy.span_end = ORIGIN + timedelta(minutes=1)
    tail = make_leaf("tail", 1, scope=replace(first.scope, session="second"))
    harness = make_harness([first, tail], profiles=event_profiles())
    result = harness.composer.build(incremental_request(
        [first, tail], now=ORIGIN + timedelta(minutes=1),
    ))
    assert result.complete
    assert not result.created_parent_ids
    assert result.deferred_child_count == 2
    assert result.pending_before == ORIGIN
    assert not harness.builder.calls


def test_scene_grouping_uses_persisted_llm_body_without_regenerating_it() -> None:
    leaves = [make_leaf("one"), make_leaf("two", 1)]
    profiles = build_profiles({"time": {
        "parent_roles": ["time_span", "scene"], "stage_options": {
            "TimeSpanMerger": {"gap_seconds": "1", "summary_mode": "llm"},
            "SceneSegmenter": {"max_duration_seconds": "3600", "similarity_threshold": "0.5"},
        },
    }})
    harness = make_harness(leaves)
    llm = RecordingLLM(['{"summary":"GENERATED FIRST"}', '{"summary":"GENERATED SECOND"}'])
    embedder = RecordingEmbedder([[1, 0], [1, 0]])
    harness.composer = DefaultHierarchyComposer(
        harness.builder, profiles, models=HierarchyModelDependencies(llm=llm, embedder=embedder),
    )
    initial_request = make_request(leaves)
    initial = harness.composer.build(initial_request)
    home = initial_request.options.tree_home_scope
    spans = [harness.read(home, uid) for uid in initial.created_parent_ids]
    assert "GENERATED FIRST" in spans[0].content
    request = incremental_request(spans, role=HierarchyRole.SCENE,
                                  support=subtree_evidence(harness, spans))
    result = harness.composer.build(request)
    assert result.complete
    assert len(result.created_parent_ids) == 1
    assert len(llm.calls) == 2, "上层结构化摘要不重复调用下层 LLM"
    assert embedder.calls == [["GENERATED FIRST", "GENERATED SECOND"]]
    for previous in spans:
        current = harness.read(home, previous.id)
        assert current.segments == previous.segments
        assert current.layers == previous.layers
        assert current.hierarchy.child_ids == previous.hierarchy.child_ids
        assert current.hierarchy.parent_id == result.created_parent_ids[0]


def test_unsettled_group_does_not_call_summary_llm() -> None:
    leaf = make_leaf("tail")
    profiles = build_profiles({"time": {
        "parent_roles": ["time_span"],
        "stage_options": {"TimeSpanMerger": {"summary_mode": "llm", "gap_seconds": "60"}},
    }})
    harness = make_harness([leaf])
    llm = RecordingLLM()
    harness.composer = DefaultHierarchyComposer(
        harness.builder, profiles, models=HierarchyModelDependencies(llm=llm),
    )
    result = harness.composer.build(incremental_request([leaf], now=ORIGIN))
    assert result.complete and result.deferred_child_count == 1
    assert not llm.calls
    assert not harness.builder.calls


@pytest.mark.parametrize("bad", [
    "jump", "string_role", "tuple_roles", "unbounded", "past_settle",
    "existing", "attached", "bad_context", "missing_profile",
])
def test_invalid_incremental_request_fails_before_writes(bad) -> None:
    leaf = make_leaf("invalid")
    profiles = None if bad == "missing_profile" else event_profiles()
    harness = make_harness([leaf], profiles=profiles)
    request = incremental_request([leaf])
    if bad == "jump":
        request.options.parent_roles = [HierarchyRole.EVENT]
    elif bad == "string_role":
        request.options.parent_roles = ["time_span"]
    elif bad == "tuple_roles":
        request.options.parent_roles = (HierarchyRole.TIME_SPAN,)
    elif bad == "unbounded":
        request.options.span_start = request.options.span_end = None
    elif bad == "past_settle":
        request.incremental.settle_at = ORIGIN
    elif bad == "existing":
        request.existing_parents = [leaf]
    elif bad == "attached":
        request.leaves[0].hierarchy.parent_id = "other"
    elif bad == "bad_context":
        request.incremental = {"settle_at": ORIGIN}
    with pytest.raises(ValidationError):
        harness.composer.build(request)
    assert not harness.builder.calls


def test_missing_middle_node_subtree_and_incremental_replace_are_rejected() -> None:
    leaf = make_leaf("one")
    harness = make_harness([leaf], profiles=event_profiles())
    request = make_request([leaf])
    initial = harness.composer.build(request)
    span = harness.read(request.options.tree_home_scope, initial.created_parent_ids[0])
    incremental = incremental_request([span], role=HierarchyRole.SCENE)
    harness.builder.calls.clear()
    with pytest.raises(ValidationError, match="完整合法子树"):
        harness.composer.build(incremental)
    with pytest.raises(ValidationError, match="不允许替换"):
        harness.composer.replace_in_span(incremental)
    assert not harness.builder.calls


@pytest.mark.parametrize("role,stage,group_key", [
    (HierarchyRole.TIME_SPAN, "TimeSpanMerger", "gap_seconds"),
    (HierarchyRole.SCENE, "SceneSegmenter", "max_duration_seconds"),
])
@pytest.mark.parametrize("group_seconds,quiet_seconds", [(7200, 60), (60, 7200)])
def test_tail_quiet_is_independent_of_group_duration(
    role, stage, group_key, group_seconds, quiet_seconds,
) -> None:
    profiles = event_profiles()
    profiles[HierarchyKind.TIME].stage_options[stage].update({
        group_key: str(group_seconds), "settle_seconds": str(quiet_seconds),
    })
    leaf = make_leaf("tail")
    harness = make_harness([leaf], profiles=profiles)
    roots, support = [leaf], []
    if role is HierarchyRole.SCENE:
        first = harness.composer.build(make_request([leaf]))
        home = make_request([leaf]).options.tree_home_scope
        roots = [harness.read(home, first.created_parent_ids[0])]
        support = subtree_evidence(harness, roots)
    harness.builder.calls.clear()
    now = ORIGIN + timedelta(seconds=quiet_seconds)
    request = incremental_request(roots, role=role, support=support, now=now)
    waiting = harness.composer.build(request)
    assert waiting.complete and waiting.deferred_child_count == 1
    assert not harness.builder.calls, "恰等于独立静默阈值时仍应等待"
    settled = harness.composer.build(incremental_request(
        roots, role=role, support=support, now=now + timedelta(microseconds=1),
    ))
    assert settled.complete and len(settled.created_parent_ids) == 1
