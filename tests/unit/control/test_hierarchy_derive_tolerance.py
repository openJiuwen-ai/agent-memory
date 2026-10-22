# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""增量候选容错、跨窗口中间节点和完整证据读取的内存回归。"""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import pytest

from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRef, HierarchyRole
from jiuwen_memory.control.jobs_impl.hierarchy_job import HierarchyJobLimits
from jiuwen_memory.control.types import JobStatus
from tests.unit.construction.event_fixtures import event_profiles, read_event_path
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_leaf, make_request
from tests.unit.control.hierarchy_derive_fixtures import DeriveClock, derive_job
from tests.unit.control.hierarchy_job_fixtures import job_harness

pytestmark = pytest.mark.unit


def test_many_expired_snapshots_do_not_use_candidate_quota_or_block_healthy_chain() -> None:
    old = [make_leaf(f"expired-{index}") for index in range(25)]
    normal = make_leaf("normal", 8 * 1440)
    harness = job_harness([*old, normal], profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(days=8, minutes=1)))
    job.limits = HierarchyJobLimits(max_leaves=1, page_size=2)
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["created_parent_count"] == "3"
    assert result.detail["skipped_outside_window_count"] == "25"
    assert result.detail["skipped_late_count"] == "0"
    assert result.detail["needs_rebuild_count"] == "25"
    assert result.detail["pending_before"] == ""
    samples = json.loads(result.detail["skipped_samples"])
    assert len(samples) == 20, "大量异常输入只能保留有界样例"
    assert samples[0]["scope"] == asdict(old[0].scope)
    assert samples[0]["role"] == "snapshot"
    assert samples[0]["reason"] == "outside_window"
    assert all(harness.read(leaf) == leaf for leaf in old)
    assert len(read_event_path(harness, normal)) == 4
    following = asyncio.run(job.run())
    assert following.status is JobStatus.SUCCEEDED, following.detail
    assert following.detail["created_parent_count"] == "0"
    assert following.detail["skipped_late_count"] == "25", "新水位建立后按迟到优先分类"


@pytest.mark.parametrize("offset", [-1, 0])
def test_late_snapshot_does_not_block_new_input_or_change_old_tree(offset) -> None:
    original = make_leaf("first")
    harness = job_harness([original], profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    assert asyncio.run(job.run()).status is JobStatus.SUCCEEDED
    old_path = read_event_path(harness, original)
    late, healthy = make_leaf("late", offset), make_leaf("healthy", 1)
    harness.composition.builder.build([late, healthy])
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["skipped_late_count"] == "1"
    assert result.detail["created_parent_count"] == "3"
    assert harness.read(late) == late
    assert read_event_path(harness, original) == old_path
    assert len(read_event_path(harness, healthy)) == 4


def test_old_middle_roots_and_full_descendants_survive_multiple_lookback_windows() -> None:
    leaf = make_leaf("early")
    profiles = event_profiles(settle_seconds=str(20 * 86400))
    profiles[HierarchyKind.TIME].stage_options["SceneSegmenter"].update(
        max_duration_seconds=str(14 * 86400), settle_seconds=str(8 * 86400),
    )
    harness = job_harness([leaf], profiles=profiles)
    clock = DeriveClock(ORIGIN + timedelta(hours=3))
    job = derive_job(harness, clock)
    first = asyncio.run(job.run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    assert first.detail["created_parent_count"] == "1"
    old_leaf = harness.read(leaf)
    old_span = harness.composition.read(harness.home, old_leaf.hierarchy.parent_id)
    assert not old_span.hierarchy.parent_id
    clock.now = ORIGIN + timedelta(days=8, seconds=1)
    second = asyncio.run(job.run())
    assert second.status is JobStatus.SUCCEEDED, second.detail
    assert second.detail["created_parent_count"] == "1"
    assert second.detail["needs_rebuild_count"] == "0"
    span = harness.composition.read(harness.home, old_span.id)
    scene = harness.composition.read(harness.home, span.hierarchy.parent_id)
    assert span.segments == old_span.segments
    assert span.hierarchy.child_ids == old_span.hierarchy.child_ids
    assert not scene.hierarchy.parent_id
    clock.now = ORIGIN + timedelta(days=20, seconds=1)
    third = asyncio.run(job.run())
    assert third.status is JobStatus.SUCCEEDED, third.detail
    assert third.detail["created_parent_count"] == "1"
    assert third.detail["needs_rebuild_count"] == "0"
    assert harness.read(leaf) == old_leaf
    assert harness.composition.read(harness.home, span.id) == span
    path = read_event_path(harness, leaf)
    assert path[2].segments == scene.segments
    last_request = harness.evolver.requests[-1]
    assert last_request.hierarchy_options.span_start <= ORIGIN
    assert {unit.id for unit in last_request.hierarchy_incremental.supporting_units} == {
        leaf.id, old_span.id,
    }


@pytest.mark.parametrize("late_role", [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE])
def test_late_middle_root_is_skipped_without_losing_its_subtree(late_role) -> None:
    original = make_leaf("original")
    harness = job_harness([original], profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    assert asyncio.run(job.run()).status is JobStatus.SUCCEEDED
    late = make_leaf("late", -10)
    harness.composition.builder.build([late])
    request = make_request([late], span=(ORIGIN - timedelta(hours=1), ORIGIN))
    if late_role is HierarchyRole.SCENE:
        request.options.parent_roles = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE]
    built = harness.composition.composer.build(request)
    assert built.complete
    old_units = [harness.read(late), *(
        harness.composition.read(harness.home, uid) for uid in built.created_parent_ids
    )]
    healthy, late_snapshot = make_leaf("healthy", 1), make_leaf("late-snapshot", -1)
    harness.composition.builder.build([healthy, late_snapshot])
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["created_parent_count"] == "3"
    samples = json.loads(result.detail["skipped_samples"])
    assert result.detail["skipped_late_count"] == "2", "跳过数应跨层累计"
    assert result.detail["needs_rebuild_count"] == "2"
    assert [sample["role"] for sample in samples] == ["snapshot", late_role.value]
    assert harness.read(late_snapshot) == late_snapshot
    assert all(harness.read(unit) == unit for unit in old_units)
    assert len(read_event_path(harness, healthy)) == 4


def test_missing_descendant_remains_a_real_failure_before_evolve() -> None:
    leaf = make_leaf("child")
    harness = job_harness([leaf], profiles=event_profiles(settle_seconds="1"))
    built = harness.composition.composer.build(make_request([leaf]))
    span = harness.composition.read(harness.home, built.created_parent_ids[0])
    span.hierarchy.child_ids = ["missing"]
    harness.overwrite(span)
    result = asyncio.run(derive_job(harness, DeriveClock(ORIGIN + timedelta(days=8))).run())
    assert result.status is JobStatus.FAILED, result.detail
    assert result.detail["complete"] == "false"
    assert not harness.evolver.requests


def test_outside_parent_claiming_selected_snapshot_fails_before_evolve() -> None:
    leaf = make_leaf("child")
    harness = job_harness([leaf], profiles=event_profiles(settle_seconds="1"))
    built = harness.composition.composer.build(make_request([leaf]))
    parent = harness.composition.read(harness.home, built.created_parent_ids[0])
    changed = deepcopy(leaf)
    changed.hierarchy.span_start += timedelta(minutes=1)
    changed.hierarchy.span_end += timedelta(minutes=1)
    harness.overwrite(changed)
    result = asyncio.run(derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5))).run())
    assert result.status is JobStatus.FAILED, result.detail
    assert "outside old parent claims" in result.detail["error"]
    assert not harness.evolver.requests
    assert harness.composition.read(harness.home, parent.id) == parent


def test_incomplete_time_is_skipped_without_quota_pending_or_repair_latch() -> None:
    missing = [make_leaf(f"missing-{index}") for index in range(21)]
    for leaf in missing:
        leaf.hierarchy.span_end = None
    plain = make_leaf("no-time")
    plain.hierarchy = HierarchyRef()
    plain.temporal.t_message = None
    harness = job_harness([*missing, plain], profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    job.limits = HierarchyJobLimits(max_leaves=1, page_size=2)
    skipped = asyncio.run(job.run())
    assert skipped.status is JobStatus.SUCCEEDED, skipped.detail
    assert skipped.detail["skipped"] == "true"
    assert skipped.detail["reason"] == "no_eligible_candidates"
    assert skipped.detail["created_parent_count"] == "0" and not job.state.needs_repair
    healthy = make_leaf("healthy", 1)
    harness.composition.builder.build([healthy])
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert result.detail["created_parent_count"] == "3"
    assert result.detail["skipped_incomplete_time_count"] == "22"
    assert result.detail["pending_before"] == "" and result.detail["needs_rebuild_count"] == "0"
    samples = json.loads(result.detail["skipped_samples"])
    assert len(samples) == 20 and samples[0]["reason"] == "incomplete_time"
    assert samples[0]["span_end"] is None
    assert all(harness.read(leaf) == leaf for leaf in [*missing, plain])
    assert len(read_event_path(harness, healthy)) == 4
    future = make_leaf("future", 10)
    harness.composition.builder.build([future])
    waiting = asyncio.run(job.run())
    assert waiting.status is JobStatus.SUCCEEDED, waiting.detail
    assert "skipped" not in waiting.detail, "有效未来输入须保留待定状态，不算全缺时跳过"
    assert waiting.detail["pending_before"] == future.hierarchy.span_start.isoformat()


@pytest.mark.parametrize("keep_reverse", [False, True])
def test_existing_parent_with_incomplete_child_still_fails_explicit_and_auto(keep_reverse) -> None:
    leaf = make_leaf("child")
    harness = job_harness([leaf], profiles=event_profiles(settle_seconds="1"))
    assert harness.composition.composer.build(make_request([leaf])).complete
    stored = harness.read(leaf)
    stored.hierarchy.span_end = None
    if not keep_reverse:
        stored.hierarchy.parent_id = ""
        stored.hierarchy.parent_scope = None
    harness.overwrite(stored)
    for job in (harness.job(), derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))):
        result = asyncio.run(job.run())
        assert result.status is JobStatus.FAILED, result.detail
    assert not harness.evolver.requests
