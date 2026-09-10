# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""三层建树通过公开 Composer/Job 验证，先保证结构与完整重建。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, LifecycleState, validate_tree
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import HierarchyJobLimits
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.types import KVMemoryListResult
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_harness, make_leaf, make_request
from tests.unit.control.hierarchy_job_fixtures import job_harness

pytestmark = pytest.mark.unit


def test_three_levels_keep_original_snapshots_and_write_roots_first() -> None:
    leaves = [make_leaf("first"), make_leaf("second", 20)]
    leaves[1].scope.session = "s2"
    harness = make_harness(leaves)
    request = make_request(leaves)
    request.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    original = deepcopy(request)

    result = harness.composer.build(request)

    assert result.complete
    assert len(result.created_parent_ids) == 3
    parents = [harness.read(request.options.tree_home_scope, uid)
               for uid in result.created_parent_ids]
    scene = parents[0]
    assert scene.hierarchy.role is HierarchyRole.SCENE
    assert scene.hierarchy.child_ids == [parent.id for parent in parents[1:]]
    assert "2 个片段" in scene.content
    assert "2 条记录" in scene.content
    current = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    validate_tree([*parents, *current])
    for persisted, untouched in zip(current, leaves):
        persisted.hierarchy = deepcopy(untouched.hierarchy)
        assert persisted == untouched
    assert request == original
    assert harness.builder.calls[0].units == [scene]


def test_scene_rebuild_completes_nonintersecting_sibling_spans_and_retires_both_layers() -> None:
    originals = [make_leaf("early"), make_leaf("late", 30)]
    originals[1].scope.session = "s2"
    harness = job_harness(originals)
    harness.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    first = asyncio.run(harness.job().run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    first_span_id = harness.read(originals[0]).hierarchy.parent_id
    first_span = harness.composition.read(harness.home, first_span_id)
    old_scene = harness.composition.read(harness.home, first_span.hierarchy.parent_id)
    old_ids = [old_scene.id, *old_scene.hierarchy.child_ids]
    harness.options.span_end = ORIGIN + timedelta(minutes=1)

    result = asyncio.run(harness.job().run())

    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["updated_child_count"] == "2"
    assert result.detail["replaced_parent_count"] == "3"
    for old_id in old_ids:
        retired = harness.composition.read(harness.home, old_id)
        assert retired.lifecycle is LifecycleState.ARCHIVED
        assert not retired.hierarchy.parent_id
        assert not retired.hierarchy.child_ids
    for original in originals:
        latest = harness.read(original)
        assert latest.hierarchy.parent_id not in old_ids


def test_composer_rejects_replacement_with_missing_scene_sibling_without_writes() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 20)]
    leaves[1].scope.session = "s2"
    harness = make_harness(leaves)
    request = make_request(leaves)
    request.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    initial = harness.composer.build(request)
    request.existing_parents = [harness.read(request.options.tree_home_scope, uid)
                                for uid in initial.created_parent_ids[:-1]]
    request.leaves = [harness.read(leaf.scope, leaf.id) for leaf in leaves]
    request.options.span_start = ORIGIN
    request.options.span_end = ORIGIN + timedelta(minutes=1)
    harness.builder.calls.clear()

    with pytest.raises(ValidationError):
        harness.composer.replace_in_span(request)

    assert not harness.builder.calls


@pytest.mark.parametrize("damage", ["missing_span", "missing_snapshot", "reverse", "scope",
                                     "inactive", "foreign_parent", "unsupported_root"])
def test_incomplete_scene_subtree_is_rejected_without_any_writes(damage) -> None:
    leaves = [make_leaf("first"), make_leaf("second", 30)]
    leaves[1].scope.session = "s2"
    harness = job_harness(leaves)
    harness.options.parent_roles.append(HierarchyRole.SCENE)
    first = asyncio.run(harness.job().run())
    assert first.status is JobStatus.SUCCEEDED
    leaf = harness.read(leaves[1])
    span = harness.composition.read(harness.home, leaf.hierarchy.parent_id)
    scene = harness.composition.read(harness.home, span.hierarchy.parent_id)
    if damage.startswith("missing"):
        from jiuwen_memory.common.type_def import memory_key
        missing = span if damage == "missing_span" else leaf
        harness.kv.delete(missing.scope, memory_key(missing.id))
    elif damage == "reverse":
        span.hierarchy.parent_id = "unknown"
        harness.overwrite(span)
    elif damage == "inactive":
        span.lifecycle = LifecycleState.ARCHIVED
        harness.overwrite(span)
    else:
        if damage == "scope":
            scene.hierarchy.child_scopes[1] = replace(harness.home, user="outsider")
        elif damage == "foreign_parent":
            scene.hierarchy.child_ids.pop()
            scene.hierarchy.child_scopes.pop()
        else:
            scene.hierarchy.parent_id = "event"
        harness.overwrite(scene)
    harness.options.span_end = ORIGIN + timedelta(minutes=1)
    harness.composition.builder.calls.clear()
    harness.evolver.requests.clear()

    result = asyncio.run(harness.job().run())

    assert result.status is JobStatus.FAILED, result.detail
    assert not harness.composition.builder.calls


@pytest.mark.parametrize("change", ["disappear", "archive"])
def test_final_scan_rejects_disappeared_or_inactive_candidate(monkeypatch, change) -> None:
    original = make_leaf("fact")
    harness = job_harness([original])
    harness.options.parent_roles.append(HierarchyRole.SCENE)
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    actual_list = harness.kv.list
    leaf_scans = []

    def changed_list(scope, *, offset=0, limit=100):
        page = actual_list(scope, offset=offset, limit=limit)
        if scope != original.scope:
            return page
        leaf_scans.append(scope)
        if len(leaf_scans) == 1:
            return page
        if change == "disappear":
            return KVMemoryListResult([], 0)
        archived = harness.read(original)
        archived.lifecycle = LifecycleState.ARCHIVED
        return KVMemoryListResult([(page.entries[0][0], dumps(archived))], 1)

    monkeypatch.setattr(harness.kv, "list", changed_list)
    harness.composition.builder.calls.clear()
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.FAILED, info.detail
    assert "completeness check" in info.detail["error"]
    assert not harness.composition.builder.calls


def test_three_level_leaf_limit_counts_out_of_window_descendants() -> None:
    leaves = [make_leaf("first"), make_leaf("second", 30)]
    leaves[1].scope.session = "s2"
    harness = job_harness(leaves)
    harness.options.parent_roles.append(HierarchyRole.SCENE)
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    harness.options.span_end = ORIGIN + timedelta(minutes=1)
    harness.composition.builder.calls.clear()
    info = asyncio.run(harness.job(HierarchyJobLimits(max_leaves=1)).run())
    assert info.status is JobStatus.FAILED
    assert "max_leaves=1" in info.detail["error"]
    assert not harness.composition.builder.calls


def test_upgrade_two_levels_but_reject_downgrading_existing_scene() -> None:
    harness = job_harness([make_leaf("fact")])
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    harness.options.parent_roles.append(HierarchyRole.SCENE)
    upgraded = asyncio.run(harness.job().run())
    assert upgraded.status is JobStatus.SUCCEEDED
    assert upgraded.detail["created_parent_count"] == "2"
    assert upgraded.detail["replaced_parent_count"] == "1"
    harness.options.parent_roles = [HierarchyRole.TIME_SPAN]
    harness.composition.builder.calls.clear()
    downgraded = asyncio.run(harness.job().run())
    assert downgraded.status is JobStatus.FAILED
    assert not harness.composition.builder.calls
