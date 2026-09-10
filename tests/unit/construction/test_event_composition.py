# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""四层构建、完整区间重建及所有坏边在写入前拒绝。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, LifecycleState, memory_key, validate_tree
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import HierarchyJobLimits
from jiuwen_memory.control.types import JobStatus
from tests.unit.construction.event_fixtures import (
    EVENT_ROLES,
    event_job_harness,
    event_profiles,
    read_event_path,
)
from tests.unit.construction.hierarchy_fixtures import (
    ORIGIN,
    make_harness,
    make_leaf,
    make_request,
    replacement_request,
)
from tests.unit.control.hierarchy_job_fixtures import job_harness

pytestmark = pytest.mark.unit


def test_four_levels_keep_same_id_cross_session_leaves_and_write_top_down() -> None:
    leaves = [make_leaf("same"), make_leaf("same", 20)]
    leaves[1].scope.session = "s2"
    leaves[1].system_metadata["infer"] = "true"
    harness = make_harness(leaves, profiles=event_profiles())
    request = make_request(leaves)
    request.options.parent_roles = list(EVENT_ROLES)
    original = deepcopy(request)
    result = harness.composer.build(request)
    assert result.complete
    parents = [harness.read(request.options.tree_home_scope, uid)
               for uid in result.created_parent_ids]
    assert [unit.hierarchy.role for unit in parents] == [
        HierarchyRole.EVENT, HierarchyRole.SCENE, HierarchyRole.SCENE,
        HierarchyRole.TIME_SPAN, HierarchyRole.TIME_SPAN,
    ]
    current = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    validate_tree([*parents, *current])
    assert len({leaf.hierarchy.parent_id for leaf in current}) == 2
    for persisted, untouched in zip(current, leaves):
        persisted.hierarchy = deepcopy(untouched.hierarchy)
        assert persisted == untouched
    assert request == original
    assert [call.units[0].hierarchy.role for call in harness.builder.calls[:3]] == [
        HierarchyRole.EVENT, HierarchyRole.SCENE, HierarchyRole.TIME_SPAN,
    ]


def test_event_gap_rebuild_completes_all_levels_outside_window_and_archives_old_parents() -> None:
    leaves = [make_leaf("early"), make_leaf("late", 30)]
    harness = event_job_harness(leaves)
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    old_paths = [read_event_path(harness, leaf) for leaf in leaves]
    old_ids = {parent.id for old_path in old_paths for parent in old_path[1:]}
    harness.options.span_start = ORIGIN + timedelta(minutes=15)
    harness.options.span_end = harness.options.span_start
    result = asyncio.run(harness.job(HierarchyJobLimits(max_leaves=2, page_size=1)).run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["updated_child_count"] == "2"
    assert result.detail["replaced_parent_count"] == "5"
    assert result.detail["created_parent_count"] == "5"
    for uid in old_ids:
        retired = harness.composition.read(harness.home, uid)
        assert retired.lifecycle is LifecycleState.ARCHIVED
        assert not retired.hierarchy.parent_id
        assert not retired.hierarchy.child_ids
        assert not retired.hierarchy.child_scopes
    for original in leaves:
        path = read_event_path(harness, original)
        assert not old_ids.intersection(unit.id for unit in path)
        assert path[0].content == original.content


@pytest.mark.parametrize("damage", [
    "missing_scene", "missing_span", "missing_snapshot", "reverse", "scope", "inactive",
    "omitted_scene", "omitted_span", "wrong_role", "nonroot_event", "duplicate_claim", "span",
])
def test_damaged_event_subtree_fails_without_writes(damage) -> None:
    leaves = [make_leaf("early"), make_leaf("late", 30)]
    harness = event_job_harness(leaves)
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    snapshot, span, scene, event = read_event_path(harness, leaves[1])
    if damage.startswith("missing"):
        missing_nodes = {"missing_scene": scene, "missing_span": span, "missing_snapshot": snapshot}
        missing = missing_nodes[damage]
        harness.kv.delete(missing.scope, memory_key(missing.id))
    elif damage == "reverse":
        scene.hierarchy.parent_id = "unknown"
        harness.overwrite(scene)
    elif damage == "inactive":
        scene.lifecycle = LifecycleState.ARCHIVED
        harness.overwrite(scene)
    elif damage == "wrong_role":
        scene.hierarchy.role = HierarchyRole.TIME_SPAN
        harness.overwrite(scene)
    elif damage == "omitted_span":
        first_scene = read_event_path(harness, leaves[0])[2]
        first_scene.hierarchy.child_ids.extend(scene.hierarchy.child_ids)
        first_scene.hierarchy.child_scopes.extend(scene.hierarchy.child_scopes)
        first_scene.hierarchy.span_end = scene.hierarchy.span_end
        scene.hierarchy.child_ids = []
        scene.hierarchy.child_scopes = []
        harness.overwrite(scene)
        harness.overwrite(first_scene)
    elif damage == "span":
        event.hierarchy.span_end = ORIGIN + timedelta(minutes=1)
        harness.overwrite(event)
    else:
        if damage == "scope":
            event.hierarchy.child_scopes[1] = replace(harness.home, user="outsider")
        elif damage == "omitted_scene":
            event.hierarchy.child_ids.pop()
            event.hierarchy.child_scopes.pop()
        elif damage == "duplicate_claim":
            event.hierarchy.child_ids.append(scene.id)
            event.hierarchy.child_scopes.append(scene.scope)
        else:
            event.hierarchy.parent_id = "unsupported"
        harness.overwrite(event)
    harness.options.span_end = ORIGIN + timedelta(minutes=1)
    harness.composition.builder.calls.clear()
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.FAILED, info.detail
    assert not harness.composition.builder.calls


@pytest.mark.parametrize("missing_position", [0, 1, 3])
def test_direct_replace_requires_event_and_all_intermediate_parents(missing_position) -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    harness = make_harness(leaves, profiles=event_profiles())
    request = make_request(leaves)
    request.options.parent_roles = list(EVENT_ROLES)
    result = harness.composer.build(request)
    replacement = replacement_request(harness, request, result.created_parent_ids)
    replacement.options.parent_roles = list(EVENT_ROLES)
    replacement.existing_parents.pop(missing_position)
    harness.builder.calls.clear()
    with pytest.raises(ValidationError):
        harness.composer.replace_in_span(replacement)
    assert not harness.builder.calls


def test_four_level_limit_counts_all_outside_window_leaves() -> None:
    harness = event_job_harness([make_leaf("early"), make_leaf("late", 30)])
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    harness.options.span_end = ORIGIN + timedelta(minutes=1)
    harness.composition.builder.calls.clear()
    info = asyncio.run(harness.job(HierarchyJobLimits(max_leaves=1)).run())
    assert info.status is JobStatus.FAILED
    assert "max_leaves=1" in info.detail["error"]
    assert not harness.composition.builder.calls


def test_three_level_upgrade_to_event_but_no_implicit_downgrade() -> None:
    harness = job_harness([make_leaf("fact")])
    harness.options.parent_roles = EVENT_ROLES[:2]
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    harness.options.parent_roles = list(EVENT_ROLES)
    upgraded = asyncio.run(harness.job().run())
    assert upgraded.status is JobStatus.SUCCEEDED, upgraded.detail
    assert upgraded.detail["created_parent_count"] == "3"
    assert upgraded.detail["replaced_parent_count"] == "2"
    harness.options.parent_roles = EVENT_ROLES[:2]
    harness.composition.builder.calls.clear()
    downgraded = asyncio.run(harness.job().run())
    assert downgraded.status is JobStatus.FAILED
    assert not harness.composition.builder.calls


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
def test_all_three_parent_layers_persist_before_snapshot_switch(fail_at) -> None:
    leaf = make_leaf("fact")
    harness = make_harness([leaf], profiles=event_profiles())
    request = make_request([leaf])
    request.options.parent_roles = list(EVENT_ROLES)
    harness.builder.fail_at = fail_at
    result = harness.composer.build(request)
    assert not result.complete
    assert len(result.created_parent_ids) == fail_at - 1
    assert not result.updated_child_ids
    assert result.repair_required
    assert harness.read(leaf.scope, leaf.id) == leaf
