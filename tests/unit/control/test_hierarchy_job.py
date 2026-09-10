# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式两层建树的真实真源闭环、完整读取、边界与失败语义。"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.lock import LockTimeoutError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    Scope,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.construction.evolver import EvolveMode, EvolveResult
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeResult, HierarchyRepair
from jiuwen_memory.control.jobs_impl.hierarchy_job import (
    HierarchyJob,
    HierarchyJobDependencies,
    HierarchyJobLimits,
    HierarchyJobSpec,
    build_spec,
)
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.types import KVMemoryListResult
from tests.unit.construction.hierarchy_evolve_fixtures import make_factory_context
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_leaf
from tests.unit.control.hierarchy_job_fixtures import ObservedEvolver, ObservedLock, job_harness

pytestmark = pytest.mark.unit


def test_first_build_uses_explicit_evolve_request_and_real_persistence() -> None:
    original = make_leaf("first")
    harness = job_harness([original])
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.SUCCEEDED, info.detail
    leaf = harness.read(original)
    parent = harness.composition.read(harness.home, leaf.hierarchy.parent_id)
    assert info.status is JobStatus.SUCCEEDED
    assert info.mode == "hierarchy"
    assert info.detail["created_parent_count"] == "1"
    assert info.detail["updated_child_count"] == "1"
    assert info.detail["complete"] == "true"
    assert info.detail["trigger"] == "explicit"
    assert parent.hierarchy.child_ids == [original.id]
    assert parent.hierarchy.child_scopes == [original.scope]
    assert leaf.segments == original.segments
    assert leaf.hierarchy.parent_scope == harness.home
    assert harness.evolver.requests[0].mode is EvolveMode.HIERARCHY
    assert harness.evolver.requests[0].metadata == {"trigger": "explicit"}


def test_rebuild_without_new_leaves_includes_out_of_window_children() -> None:
    originals = [make_leaf("a", 0), make_leaf("b", 30)]
    harness = job_harness(originals)
    first = asyncio.run(harness.job().run())
    old_parent_id = harness.read(originals[0]).hierarchy.parent_id
    harness.options.span_end = ORIGIN + timedelta(minutes=1)
    second = asyncio.run(harness.job().run())
    old_parent = harness.composition.read(harness.home, old_parent_id)
    latest = [harness.read(original) for original in originals]
    assert first.status is second.status is JobStatus.SUCCEEDED
    assert second.detail["updated_child_count"] == "2"
    assert second.detail["replaced_parent_count"] == "1"
    assert latest[0].hierarchy.parent_id == latest[1].hierarchy.parent_id
    assert latest[0].hierarchy.parent_id != old_parent_id
    assert old_parent.lifecycle is LifecycleState.ARCHIVED
    assert old_parent.hierarchy.child_ids == []


def test_cross_session_scopes_and_same_ids_remain_distinct() -> None:
    first = make_leaf("same")
    second = make_leaf("same", scope=replace(first.scope, session="s2"))
    second.system_metadata["infer"] = "true"
    harness = job_harness([first, second])
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.detail["created_parent_count"] == "2"
    assert info.detail["updated_child_count"] == "2"
    assert harness.read(first).hierarchy.parent_id != harness.read(second).hierarchy.parent_id


def test_pagination_reads_past_unrelated_units(monkeypatch) -> None:
    unrelated = [make_leaf(f"plain-{index}") for index in range(5)]
    for plain in unrelated:
        plain.hierarchy = HierarchyRef()
    target = make_leaf("z-target")
    harness = job_harness([*unrelated, target])
    original_list = harness.kv.list
    offsets: list[int] = []

    def recorded_list(scope, *, offset=0, limit=100):
        offsets.append(offset)
        return original_list(scope, offset=offset, limit=limit)

    monkeypatch.setattr(harness.kv, "list", recorded_list)
    info = asyncio.run(harness.job(HierarchyJobLimits(page_size=2)).run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.detail["updated_child_count"] == "1"
    assert 4 in offsets


@pytest.mark.parametrize("failure", ["stalled", "duplicate", "changed_count", "too_many", "bad_id"])
def test_incomplete_or_invalid_pagination_fails_without_writes(monkeypatch, failure) -> None:
    harness = job_harness()
    single = harness.kv.list(make_leaf("unused").scope).entries[0]

    def invalid_list(scope, *, offset=0, limit=100):
        if scope == harness.home:
            return KVMemoryListResult()
        if failure == "stalled":
            return KVMemoryListResult([], 1)
        if failure == "duplicate":
            return KVMemoryListResult([single], 2)
        if failure == "changed_count":
            return KVMemoryListResult([single], 2 if offset == 0 else 3)
        if failure == "too_many":
            return KVMemoryListResult([single], 0)
        return KVMemoryListResult([("/memory/wrong", single[1])], 1)

    monkeypatch.setattr(harness.kv, "list", invalid_list)
    info = asyncio.run(harness.job(HierarchyJobLimits(page_size=1)).run())
    assert info.status is JobStatus.FAILED
    assert info.detail["complete"] == "false"
    assert not harness.evolver.requests
    assert not harness.composition.builder.calls


@pytest.mark.parametrize("damage", ["missing", "reverse", "inactive", "dismissed", "kind", "role"])
def test_invalid_old_children_fail_without_writes(damage) -> None:
    original = make_leaf("child")
    harness = job_harness([original])
    first = asyncio.run(harness.job().run())
    child = harness.read(original)
    if damage == "missing":
        harness.kv.delete(child.scope, memory_key(child.id))
    else:
        if damage == "reverse":
            child.hierarchy.parent_id = "another-parent"
        elif damage == "inactive":
            child.lifecycle = LifecycleState.ARCHIVED
        elif damage == "dismissed":
            child.hierarchy.status = HierarchyStatus.DISMISSED
        elif damage == "kind":
            child.hierarchy.kind = HierarchyKind.TOPIC
        else:
            child.hierarchy.role = HierarchyRole.TIME_SPAN
        harness.overwrite(child)
    harness.composition.builder.calls.clear()
    harness.evolver.requests.clear()
    result = asyncio.run(harness.job().run())
    assert first.status is JobStatus.SUCCEEDED
    assert result.status is JobStatus.FAILED
    assert not harness.evolver.requests
    assert not harness.composition.builder.calls


def test_unknown_parent_on_intersecting_leaf_is_not_silently_skipped() -> None:
    dirty = make_leaf("dirty")
    dirty.hierarchy.parent_id = "missing-parent"
    harness = job_harness([dirty])
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.FAILED
    assert "unknown" in result.detail["error"]
    assert not harness.evolver.requests


def test_out_of_scope_child_reference_is_rejected_before_point_read(monkeypatch) -> None:
    original = make_leaf("child")
    harness = job_harness([original])
    asyncio.run(harness.job().run())
    parent_id = harness.read(original).hierarchy.parent_id
    parent = harness.composition.read(harness.home, parent_id)
    parent.hierarchy.child_scopes = [replace(original.scope, user="outsider")]
    harness.overwrite(parent)
    reads: list[Scope] = []
    actual_mget = harness.kv.mget

    def observed_mget(scope, keys):
        reads.append(scope)
        return actual_mget(scope, keys)

    monkeypatch.setattr(harness.kv, "mget", observed_mget)
    harness.composition.builder.calls.clear()
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.FAILED
    assert not reads
    assert not harness.composition.builder.calls


@pytest.mark.parametrize("field", ["org", "space", "user", "agent"])
def test_candidate_listing_respects_exact_home_boundaries(field) -> None:
    selected = make_leaf("selected")
    excluded = deepcopy(selected)
    excluded.id = "excluded"
    setattr(excluded.scope, field, "outsider")
    harness = job_harness([selected, excluded])
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.SUCCEEDED
    assert result.detail["updated_child_count"] == "1"
    assert harness.read(excluded).hierarchy.parent_id == ""


def test_empty_org_space_are_exact_not_wildcard() -> None:
    local = make_leaf("local", scope=Scope(user="alice", agent="assistant", session="s1"))
    remote = make_leaf("remote", scope=replace(local.scope, org="remote"))
    spaced = make_leaf("spaced", scope=replace(local.scope, space="remote-space"))
    harness = job_harness([local, remote, spaced])
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.detail["updated_child_count"] == "1"
    assert harness.read(remote).hierarchy.parent_id == ""
    assert harness.read(spaced).hierarchy.parent_id == ""


@pytest.mark.parametrize("rebuilding", [False, True])
def test_leaf_limit_rejects_instead_of_truncating(rebuilding) -> None:
    harness = job_harness([make_leaf("a"), make_leaf("b", 1)])
    if rebuilding:
        asyncio.run(harness.job().run())
    harness.composition.builder.calls.clear()
    harness.evolver.requests.clear()
    info = asyncio.run(harness.job(HierarchyJobLimits(max_leaves=1)).run())
    assert info.status is JobStatus.FAILED
    assert "max_leaves=1" in info.detail["error"]
    assert not harness.evolver.requests
    assert not harness.composition.builder.calls


def test_empty_candidates_succeed_without_composer_or_messages() -> None:
    harness = job_harness([])
    harness.kv.insert(harness.home, "/messages/raw", dumps(make_leaf("raw")))
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.detail["complete"] == "true"
    assert info.detail["reason"] == "no candidates"
    assert not harness.evolver.requests


@pytest.mark.parametrize("outcome", ["missing", "incomplete", "repair"])
def test_missing_partial_or_repair_results_are_failed(outcome) -> None:
    harness = job_harness()
    harness.evolver.delegate = None
    if outcome == "incomplete":
        harness.evolver.response = EvolveResult(
            hierarchy_result=HierarchyComposeResult(complete=False, created_parent_ids=["p"]),
        )
    elif outcome == "repair":
        harness.evolver.response = EvolveResult(
            hierarchy_result=HierarchyComposeResult(
                repair_required=[HierarchyRepair("p", "index failure")], complete=True,
            ),
        )
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.FAILED
    assert info.detail["complete"] == "false"
    if outcome == "repair":
        assert info.detail["repair_required_count"] == "1"
        assert json.loads(info.detail["repair_required"])[0]["issue"] == "index failure"
    elif outcome == "incomplete":
        assert info.detail["created_parent_count"] == "1"


def test_real_index_failure_keeps_body_and_reports_failed_job() -> None:
    original = make_leaf("leaf")
    harness = job_harness([original])
    harness.composition.builder.fail_at = 3
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.FAILED
    assert info.detail["created_parent_count"] == "1"
    assert int(info.detail["repair_required_count"]) > 0
    assert harness.read(original).hierarchy.parent_id


def test_lock_timeout_fails_without_reading_or_evolving(monkeypatch) -> None:
    harness = job_harness()
    locking = ObservedLock()
    reads: list[bool] = []

    def record_scopes():
        reads.append(True)
        return []

    async def contend():
        holder = await locking.acquire(harness.home, "hierarchy:time")
        try:
            return await asyncio.create_task(
                harness.job(HierarchyJobLimits(lock_wait_ms=0), locking).run(),
            )
        finally:
            await locking.release(holder)

    monkeypatch.setattr(harness.kv, "scopes", record_scopes)
    result = asyncio.run(contend())
    assert result.status is JobStatus.FAILED
    assert "LockTimeoutError" in result.detail["error"]
    assert not reads
    assert not harness.evolver.requests


@pytest.mark.parametrize("lose_before", [False, True])
def test_lock_loss_never_reports_success(lose_before) -> None:
    harness = job_harness()
    locking = ObservedLock()
    locking.lose_at_acquire = lose_before
    if not lose_before:
        harness.evolver.before_evolve = locking.lose
    info = asyncio.run(harness.job(lock=locking).run())
    assert info.status is JobStatus.FAILED
    assert "LockLostError" in info.detail["error"]
    assert "not rolled back" in info.detail["error"]
    if lose_before:
        assert not harness.evolver.requests
    else:
        assert info.detail["created_parent_count"] == "1"


def test_job_captures_scope_and_options_at_submission() -> None:
    harness = job_harness()
    job = harness.job()
    harness.options.span_start = ORIGIN + timedelta(days=10)
    harness.options.tree_home_scope.user = "changed"
    info = asyncio.run(job.run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.scope.user == "alice"
    assert info.detail["span_start"] == ORIGIN.isoformat()
    assert job.interval == 0


def test_mutating_job_public_scope_is_rejected_before_writes() -> None:
    harness = job_harness()
    job = harness.job()
    job.scope.user = "changed"
    info = asyncio.run(job.run())
    assert info.status is JobStatus.FAILED
    assert not harness.evolver.requests


@pytest.mark.parametrize("invalid", [{"max_leaves": 0}, {"page_size": True}, {"lock_wait_ms": -1}])
def test_limits_validate_without_boolean_coercion(invalid) -> None:
    with pytest.raises(ValidationError):
        HierarchyJobLimits(**invalid)


def test_spec_requires_runtime_evolver_and_rejects_unknown_arguments() -> None:
    harness = job_harness()
    specification = HierarchyJobSpec(harness.kv)
    with pytest.raises(ValidationError, match="Engine Evolver"):
        specification.with_scope(harness.home, options=harness.options)
    with pytest.raises(ValidationError, match="unknown"):
        specification.with_scope(
            harness.home, options=harness.options, evolver=harness.evolver, interval=60,
        )


def test_spec_runtime_kv_override_wins_over_configuration() -> None:
    harness = job_harness()
    specification = HierarchyJobSpec(InMemoryKVStore())
    job = specification.with_scope(
        harness.home, options=harness.options, evolver=harness.evolver, kv=harness.kv,
    )
    info = asyncio.run(job.run())
    assert info.status is JobStatus.SUCCEEDED
    assert info.detail["updated_child_count"] == "1"


def test_direct_job_scope_must_match_home() -> None:
    harness = job_harness()
    with pytest.raises(ValidationError):
        HierarchyJob(
            replace(harness.home, session="specific"),
            HierarchyJobDependencies(harness.kv, ObservedEvolver()), harness.options,
        )


@pytest.mark.parametrize(
    "broken_field", ["role", "status", "span_start", "child_scopes", "parent_id"],
)
def test_corrupt_time_hierarchy_cannot_disappear_through_tolerant_decode(broken_field) -> None:
    original = make_leaf("corrupt")
    harness = job_harness([original])
    record = json.loads(dumps(original))
    record["hierarchy"][broken_field] = 12
    harness.kv.update(original.scope, memory_key(original.id), json.dumps(record).encode())
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.FAILED
    assert not harness.evolver.requests


def test_old_parent_cannot_claim_the_same_child_twice() -> None:
    original = make_leaf("child")
    harness = job_harness([original])
    asyncio.run(harness.job().run())
    parent_id = harness.read(original).hierarchy.parent_id
    copied_parent = harness.composition.read(harness.home, parent_id)
    copied_parent.id = "duplicate-parent"
    harness.kv.insert(harness.home, memory_key(copied_parent.id), dumps(copied_parent))
    harness.evolver.requests.clear()
    harness.composition.builder.calls.clear()
    info = asyncio.run(harness.job().run())
    assert info.status is JobStatus.FAILED
    assert "more than one" in info.detail["error"]
    assert not harness.evolver.requests
    assert not harness.composition.builder.calls


def test_in_window_leaf_missing_from_old_parent_children_fails() -> None:
    originals = [make_leaf("a"), make_leaf("b", 1)]
    harness = job_harness(originals)
    asyncio.run(harness.job().run())
    parent_id = harness.read(originals[0]).hierarchy.parent_id
    parent = harness.composition.read(harness.home, parent_id)
    parent.hierarchy.child_ids = parent.hierarchy.child_ids[:1]
    parent.hierarchy.child_scopes = parent.hierarchy.child_scopes[:1]
    harness.overwrite(parent)
    harness.evolver.requests.clear()
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.FAILED
    assert not harness.evolver.requests


@pytest.mark.parametrize("mget_failure", ["incomplete", "wrong_scope", "wrong_id"])
def test_bulk_child_read_validates_count_and_full_identity(monkeypatch, mget_failure) -> None:
    original = make_leaf("child")
    harness = job_harness([original])
    asyncio.run(harness.job().run())
    child = harness.read(original)
    if mget_failure == "wrong_scope":
        child.scope.user = "outsider"
    elif mget_failure == "wrong_id":
        child.id = "other"

    def invalid_mget(scope, keys):
        return [] if mget_failure == "incomplete" else [dumps(child)]

    monkeypatch.setattr(harness.kv, "mget", invalid_mget)
    harness.evolver.requests.clear()
    result = asyncio.run(harness.job().run())
    assert result.status is JobStatus.FAILED
    assert not harness.evolver.requests


def test_lock_remains_held_until_cancelled_sync_evolve_finishes() -> None:
    harness = job_harness()
    locking = ObservedLock()
    started = Event()
    finish = Event()

    def block_evolve():
        started.set()
        if not finish.wait(timeout=5):
            raise RuntimeError("test did not release blocked Evolver")

    async def cancel_during_evolve():
        execution = asyncio.create_task(harness.job(lock=locking).run())
        try:
            ready = await asyncio.to_thread(started.wait, 2)
            if not ready:
                pytest.fail("Evolver did not start while lock was held")
            execution.cancel()
            await asyncio.sleep(0)
            with pytest.raises(LockTimeoutError, match="仍未获得锁"):
                await locking.acquire(harness.home, "hierarchy:time", wait_timeout_ms=0)
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        fresh = await locking.acquire(harness.home, "hierarchy:time", wait_timeout_ms=0)
        await locking.release(fresh)

    harness.evolver.before_evolve = block_evolve
    asyncio.run(cancel_during_evolve())
    assert len(harness.evolver.requests) == 1
    assert len(harness.composition.builder.calls) == 4


def test_factory_spec_only_resolves_read_configuration() -> None:
    context = make_factory_context()
    configuration = ComponentConfig(
        params={
            "hierarchy_max_leaves": 25,
            "hierarchy_page_size": 4,
            "hierarchy_lock_wait_ms": 7,
            "evolver": "intentionally-unavailable",
        },
        ctx=context,
    )
    specification = build_spec(configuration)
    assert specification.limits == HierarchyJobLimits(25, 4, 7)
    assert specification.lock is None
    with pytest.raises(ValidationError, match="Engine Evolver"):
        specification.with_scope(Scope())
