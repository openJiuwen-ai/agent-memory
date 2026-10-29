# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""逐层增量、静默封口、水位、限额、锁及失败闸门的真实内存闭环。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRef, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.control.jobs_impl.hierarchy_derive_job import (
    HierarchyDeriveJobSpec,
    HierarchyDeriveOptions,
)
from jiuwen_memory.control.jobs_impl.hierarchy_job import HierarchyJobLimits, HierarchyJobSpec
from jiuwen_memory.control.types import JobStatus
from tests.unit.construction.event_fixtures import event_profiles, read_event_path
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_leaf
from tests.unit.control.hierarchy_derive_fixtures import DeriveClock, derive_job
from tests.unit.control.hierarchy_job_fixtures import ObservedLock, job_harness

pytestmark = pytest.mark.unit


def test_full_chain_incremental_then_noop_and_new_input_preserves_old_tree() -> None:
    original = make_leaf("a")
    harness = job_harness([original], profiles=event_profiles(settle_seconds="1"))
    clock = DeriveClock(ORIGIN + timedelta(seconds=2))
    job = derive_job(harness, clock)
    first = asyncio.run(job.run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    assert first.detail["created_parent_count"] == "3"
    old_path = read_event_path(harness, original)
    requests_before = len(harness.evolver.requests)
    calls_before = len(harness.composition.builder.calls)
    idle = asyncio.run(job.run())
    assert idle.status is JobStatus.SUCCEEDED, idle.detail
    assert idle.detail["created_parent_count"] == "0"
    assert len(harness.evolver.requests) == requests_before
    assert len(harness.composition.builder.calls) == calls_before

    added = make_leaf("b", 1)
    harness.composition.builder.build([added])
    clock.now = ORIGIN + timedelta(minutes=1, seconds=2)
    next_round = asyncio.run(job.run())
    assert next_round.status is JobStatus.SUCCEEDED, next_round.detail
    assert next_round.detail["created_parent_count"] == "3"
    assert read_event_path(harness, original) == old_path
    assert read_event_path(harness, added)[-1].id != old_path[-1].id
    assert harness.read(added).segments == added.segments
    assert [request.hierarchy_options.leaf_role for request in harness.evolver.requests] == [
        HierarchyRole.SNAPSHOT, HierarchyRole.TIME_SPAN, HierarchyRole.SCENE,
        HierarchyRole.SNAPSHOT, HierarchyRole.TIME_SPAN, HierarchyRole.SCENE,
    ]


def test_continuous_snapshots_commit_bounded_groups_without_waiting_for_silence() -> None:
    leaves = [make_leaf(str(index), index * 60) for index in range(7)]
    profiles = build_profiles({"time": {"parent_roles": ["time_span"]}})
    harness = job_harness(leaves[:3], profiles=profiles)
    clock = DeriveClock(ORIGIN + timedelta(hours=2))
    job = derive_job(harness, clock)

    pending = asyncio.run(job.run())
    assert pending.status is JobStatus.SUCCEEDED, pending.detail
    assert pending.detail["created_parent_count"] == "0"
    assert all(not harness.read(leaf).hierarchy.parent_id for leaf in leaves[:3])

    harness.composition.builder.build([leaves[3]])
    clock.now = ORIGIN + timedelta(hours=3)
    first = asyncio.run(job.run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    assert first.detail["created_parent_count"] == "1"
    parent_id = harness.read(leaves[0]).hierarchy.parent_id
    parent = harness.composition.read(harness.home, parent_id)
    assert parent.hierarchy.child_ids == ["0", "1", "2"]
    assert parent.hierarchy.span_start == ORIGIN
    assert parent.hierarchy.span_end == ORIGIN + timedelta(hours=2)
    assert not harness.read(leaves[3]).hierarchy.parent_id

    harness.composition.builder.build(leaves[4:])
    clock.now = ORIGIN + timedelta(hours=6)
    second = asyncio.run(job.run())
    assert second.status is JobStatus.SUCCEEDED, second.detail
    assert second.detail["created_parent_count"] == "1"
    second_id = harness.read(leaves[3]).hierarchy.parent_id
    assert harness.composition.read(harness.home, second_id).hierarchy.child_ids == ["3", "4", "5"]
    assert harness.composition.read(harness.home, parent_id) == parent
    assert not harness.read(leaves[6]).hierarchy.parent_id

    clock.now = ORIGIN + timedelta(hours=8)
    still_pending = asyncio.run(job.run())
    assert still_pending.status is JobStatus.SUCCEEDED, still_pending.detail
    assert still_pending.detail["created_parent_count"] == "0"
    clock.now += timedelta(microseconds=1)
    settled = asyncio.run(job.run())
    assert settled.status is JobStatus.SUCCEEDED, settled.detail
    assert settled.detail["created_parent_count"] == "1"
    last_id = harness.read(leaves[6]).hierarchy.parent_id
    assert harness.composition.read(harness.home, last_id).hierarchy.child_ids == ["6"]
    assert harness.composition.read(harness.home, parent_id) == parent


def test_event_tail_waits_for_strict_quiet_period_without_rebuilding_lower_layers() -> None:
    original = make_leaf("tail")
    harness = job_harness([original], profiles=event_profiles(settle_seconds="60"))
    clock = DeriveClock(ORIGIN + timedelta(seconds=60))
    job = derive_job(harness, clock)
    first = asyncio.run(job.run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    assert first.detail["created_parent_count"] == "2"
    span = harness.composition.read(harness.home, harness.read(original).hierarchy.parent_id)
    scene = harness.composition.read(harness.home, span.hierarchy.parent_id)
    assert scene.hierarchy.parent_id == ""
    assert first.detail["deferred_child_count"] == "1"
    clock.now += timedelta(seconds=1)
    second = asyncio.run(job.run())
    assert second.status is JobStatus.SUCCEEDED, second.detail
    assert second.detail["created_parent_count"] == "1"
    assert harness.composition.read(harness.home, span.id) == span
    assert read_event_path(harness, original)[2].id == scene.id


def test_failure_latches_state_and_stops_higher_layers_and_future_rounds() -> None:
    harness = job_harness(profiles=event_profiles(settle_seconds="1"))
    harness.composition.builder.fail_at = 2
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    first = asyncio.run(job.run())
    assert first.status is JobStatus.FAILED, first.detail
    assert first.detail["repair_required_count"] != "0"
    assert len(harness.evolver.requests) == 1
    assert job.state.needs_repair
    harness.composition.builder.fail_at = None
    second = asyncio.run(job.run())
    assert second.status is JobStatus.FAILED
    assert second.detail["paused_reason"] == "repair before runtime restart"
    assert second.detail["repair_required"] == first.detail["repair_required"]
    assert second.detail["created_parent_count"] == first.detail["created_parent_count"]
    assert len(harness.evolver.requests) == 1


@pytest.mark.parametrize("key", ["hierarchy.enabled", "hierarchy.auto_derive"])
def test_live_policy_disable_stops_round_before_reads_or_writes(key) -> None:
    harness = job_harness(profiles=event_profiles())
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(days=4)))
    job.dependencies.policy.set(key, "false")
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED
    assert result.detail["reason"] == "auto_derive disabled"
    assert not harness.evolver.requests
    assert not harness.composition.builder.calls


def test_no_profile_means_no_implicit_default_chain() -> None:
    harness = job_harness()
    result = asyncio.run(derive_job(harness, DeriveClock(ORIGIN)).run())
    assert result.status is JobStatus.SUCCEEDED
    assert result.detail["reason"] == "no compose profile"
    assert not harness.evolver.requests


def test_only_active_time_candidates_in_authorized_scope_regardless_of_infer() -> None:
    first = make_leaf("same")
    second = make_leaf("same", scope=replace(first.scope, session="s2"))
    second.system_metadata["infer"] = "true"
    plain = make_leaf("plain")
    plain.hierarchy = HierarchyRef()
    outsider = make_leaf("outside", scope=replace(first.scope, user="bob"))
    harness = job_harness([first, second, plain, outsider], profiles=event_profiles(
        settle_seconds="1",
    ))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    job.limits = HierarchyJobLimits(page_size=1)
    result = asyncio.run(job.run())
    assert result.status is JobStatus.SUCCEEDED, result.detail
    assert harness.read(first).hierarchy.parent_id != harness.read(second).hierarchy.parent_id
    assert harness.read(plain) == plain
    assert harness.read(outsider) == outsider


def test_lost_lock_prevents_work_and_scope_mutation_is_rejected() -> None:
    harness = job_harness(profiles=event_profiles())
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    lock = ObservedLock()
    lock.lose_at_acquire = True
    job.dependencies.hierarchy = replace(job.dependencies.hierarchy, lock=lock)
    result = asyncio.run(job.run())
    assert result.status is JobStatus.FAILED
    assert "LockLostError" in result.detail["error"]
    assert not harness.evolver.requests
    job.scope = replace(job.scope, agent="other")
    mutated = asyncio.run(job.run())
    assert mutated.status is JobStatus.FAILED
    assert "Scope changed" in mutated.detail["error"]


def test_profile_return_is_a_snapshot_not_a_mutable_control_copy() -> None:
    harness = job_harness(profiles=event_profiles())
    first = harness.evolver.hierarchy_profile(HierarchyKind.TIME)
    original = deepcopy(first)
    first.stage_options["EventBuilder"]["settle_seconds"] = "1"
    assert harness.evolver.hierarchy_profile(HierarchyKind.TIME) == original


@pytest.mark.parametrize("value", [0, -1, True, "1", 1.5])
def test_invalid_period_or_lookback_is_rejected(value) -> None:
    with pytest.raises(ValidationError):
        HierarchyDeriveOptions(interval=value)
    with pytest.raises(ValidationError):
        HierarchyDeriveOptions(lookback_seconds=value)


def test_watermark_includes_all_pages_not_only_first_parent() -> None:
    leaves = [make_leaf("a"), make_leaf("b", 1), make_leaf("c", 2)]
    harness = job_harness(leaves, profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    job.limits = HierarchyJobLimits(page_size=1)
    first = asyncio.run(job.run())
    assert first.status is JobStatus.SUCCEEDED, first.detail
    late = make_leaf("late", 1)
    harness.composition.builder.build([late])
    harness.composition.builder.calls.clear()
    second = asyncio.run(job.run())
    assert second.status is JobStatus.SUCCEEDED, second.detail
    assert second.detail["needs_rebuild_count"] == "1"
    assert second.detail["skipped_late_count"] == "1"
    assert not harness.composition.builder.calls


def test_input_change_between_initial_scan_and_collection_rejects_before_evolve(
    monkeypatch,
) -> None:
    """两次读到同一个 id 但内容不同，不继续写边。"""
    original = make_leaf("changed")
    harness = job_harness([original], profiles=event_profiles())
    original_list = harness.kv.list
    reads = 0

    def changing_list(scope, *, offset=0, limit=100):
        nonlocal reads
        if scope == original.scope:
            reads += 1
            if reads == 2:
                changed = deepcopy(original)
                changed.segments[0].content = "changed after watermark scan"
                harness.overwrite(changed)
        return original_list(scope, offset=offset, limit=limit)

    monkeypatch.setattr(harness.kv, "list", changing_list)
    result = asyncio.run(derive_job(harness, DeriveClock(ORIGIN + timedelta(days=4))).run())
    assert result.status is JobStatus.FAILED, result.detail
    assert "changed during completeness check" in result.detail["error"]
    assert not harness.evolver.requests


def test_cancel_waits_for_composition_thread_before_releasing_lock() -> None:
    harness = job_harness(profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    lock = ObservedLock()
    job.dependencies.hierarchy = replace(job.dependencies.hierarchy, lock=lock)
    entered, released = Event(), Event()

    def pause_evolve() -> None:
        entered.set()
        if not released.wait(timeout=5):
            pytest.fail("test composition was not released within timeout")

    harness.evolver.before_evolve = pause_evolve

    async def exercise() -> None:
        task = asyncio.create_task(job.run())
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.02)
            if task.done():
                pytest.fail("cancelled job released its lock while composition was still running")
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            released.set()
            if not task.done():
                await task

    asyncio.run(exercise())
    assert job.state.needs_repair, "取消后即使线程写完也需要人工核对，不自动推进上层"
    assert len(harness.evolver.requests) == 1
    assert lock.handle is not None


def test_spec_requires_same_source_dependencies_and_preserves_fault_latch() -> None:
    harness = job_harness(profiles=event_profiles())
    prototype = derive_job(harness, DeriveClock(ORIGIN))
    spec = HierarchyDeriveJobSpec(HierarchyJobSpec(harness.kv))
    with pytest.raises(ValidationError, match="runtime"):
        spec.with_scope(harness.home, evolver=harness.evolver)
    with pytest.raises(ValidationError, match="runtime"):
        spec.with_scope(harness.home, evolver=harness.evolver,
                        kv=harness.kv, policy=prototype.dependencies.policy)
    with pytest.raises(ValidationError, match="callable admission"):
        spec.with_scope(harness.home, evolver=harness.evolver,
                        kv=harness.kv, policy=prototype.dependencies.policy, admission=False)
    first = spec.with_scope(harness.home, evolver=harness.evolver,
                            kv=harness.kv, policy=prototype.dependencies.policy,
                            admission=prototype.dependencies.admission)
    first.state.needs_repair = True
    again = spec.with_scope(harness.home, evolver=harness.evolver,
                            kv=harness.kv, policy=prototype.dependencies.policy,
                            admission=prototype.dependencies.admission)
    result = asyncio.run(again.run())
    assert again.dependencies.hierarchy.kv is harness.kv
    assert again.dependencies.hierarchy.evolver is harness.evolver
    assert result.status is JobStatus.FAILED
    assert not harness.evolver.requests


@pytest.mark.parametrize(
    "phase", ["before_read", "before_evolve", "after_preparation", "after_parent"],
)
def test_admission_denial_stops_work_and_preserves_partial_write_state(phase, monkeypatch):
    from jiuwen_memory.common.errors import PermissionDeniedError

    harness = job_harness(profiles=event_profiles(settle_seconds="1"))
    job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
    denied = phase == "before_read"

    def admission(scope):
        assert scope.org == harness.home.org and scope.space == harness.home.space
        if denied:
            raise PermissionDeniedError("admission withdrawn")

    job.dependencies.admission = admission
    if phase == "before_read":
        def unexpected_read(*args, **kwargs):
            pytest.fail("denied job must not read source data")

        monkeypatch.setattr(harness.kv, "scopes", unexpected_read)
    elif phase == "before_evolve":
        from jiuwen_memory.control.jobs_impl import hierarchy_derive_job
        collect = hierarchy_derive_job.collect_incremental

        def deny_after_read(*args):
            nonlocal denied
            selected = collect(*args)
            denied = True
            return selected

        monkeypatch.setattr(hierarchy_derive_job, "collect_incremental", deny_after_read)
    elif phase == "after_preparation":
        composer = harness.composition.composer
        pipeline = composer._pipeline()  # pylint: disable=protected-access
        prepare = pipeline.build_incremental

        def deny_after_preparation(*args):
            nonlocal denied
            batch = prepare(*args)
            denied = True
            return batch

        monkeypatch.setattr(pipeline, "build_incremental", deny_after_preparation)
        monkeypatch.setattr(composer, "_pipeline", lambda: pipeline)
    else:
        builder = harness.composition.builder
        build = builder.build

        def deny_after_parent(*args, **kwargs):
            nonlocal denied
            build(*args, **kwargs)
            denied = True

        monkeypatch.setattr(builder, "build", deny_after_parent)
    result = asyncio.run(job.run())
    assert result.status is JobStatus.FAILED and result.detail["complete"] == "false"
    if phase == "after_parent":
        assert result.detail["created_parent_count"] == "1"
        assert result.detail["updated_child_count"] == "0"
        assert int(result.detail["repair_required_count"]) > 0 and job.state.needs_repair
    else:
        assert not harness.composition.builder.calls
        assert result.detail["created_parent_count"] == result.detail["updated_child_count"] == "0"
        assert not job.state.needs_repair, "准入拒绝且确认未写入时不得触发永久故障保护"
