# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""新版 DomainStore 与协作式取消接入，观察公开端口与持久化结果。"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.jobs import JobCancelledError, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.hierarchy_job import HierarchyJobSpec
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import EvolveTaskOptions, JobStatus
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.types import MemoryListResult
from tests.unit.construction.event_fixtures import event_profiles
from tests.unit.construction.hierarchy_fixtures import ORIGIN, make_leaf
from tests.unit.control.hierarchy_derive_fixtures import DeriveClock, derive_job
from tests.unit.control.hierarchy_job_fixtures import ObservedLock, job_harness

pytestmark = pytest.mark.unit


def test_cloud_engine_reads_domain_source_and_completes_outside_window_children():
    leaves = [make_leaf("first", 0), make_leaf("second", 30)]
    harness = job_harness(leaves)
    assert asyncio.run(harness.job().run()).status is JobStatus.SUCCEEDED
    domain = Mock(spec=DomainStore)

    def list_units(scope, *, offset=0, limit=100):
        page = harness.kv.list(scope, offset=offset, limit=limit)
        return MemoryListResult(items=[loads(raw) for _, raw in page.entries], count=page.count)

    def get_units(scope, ids):
        return [harness.composition.read(scope, uid) for uid in reversed(ids)]

    domain.scopes.side_effect = harness.kv.scopes
    domain.list.side_effect = list_units
    domain.get.side_effect = get_units
    unrelated_kv = Mock(spec=KVStore)
    unrelated_kv.list.side_effect = AssertionError("cloud must read DomainStore")
    unrelated_kv.scopes.side_effect = AssertionError("cloud must enumerate DomainStore")
    factory = JobFactory()
    factory.register(JobType.HIERARCHY, HierarchyJobSpec(unrelated_kv).with_scope)
    scheduler = InProcessScheduler()
    engine = CloudEngine(None, harness.composition.builder, None, domain, scheduler,
                         harness.evolver, None, kv=unrelated_kv, job_factory=factory,
                         policy=DictPolicyManager({"hierarchy.enabled": "true"}))
    options = replace(harness.options, span_end=harness.options.span_start)
    task = EvolveTaskOptions(EvolveMode.HIERARCHY, hierarchy_options=options)
    job_id = asyncio.run(engine.evolve_v2(harness.home, task))
    info = scheduler.status(job_id)
    assert info.status is JobStatus.SUCCEEDED, info.detail
    assert info.detail["updated_child_count"] == "2"
    assert domain.get.called and domain.list.called
    unrelated_kv.list.assert_not_called()
    parent_ids = {harness.read(leaf).hierarchy.parent_id for leaf in leaves}
    assert len(parent_ids) == 1


@pytest.mark.parametrize("incremental", [False, True])
def test_cooperative_cancel_before_work_leaves_storage_untouched(incremental):
    leaf = make_leaf("first")
    harness = job_harness([leaf])
    job = derive_job(harness, DeriveClock(ORIGIN)) if incremental else harness.job()
    job.request_cancel()
    with pytest.raises(JobCancelledError):
        asyncio.run(job.run())
    assert not harness.evolver.requests
    assert harness.read(leaf) == leaf


@pytest.mark.parametrize("incremental", [False, True])
def test_cooperative_cancel_waits_for_worker_and_preserves_completed_write_diagnostics(incremental):
    leaf = make_leaf("first")
    harness = job_harness([leaf], profiles=event_profiles(settle_seconds="1"))
    started, release = Event(), Event()
    lock = ObservedLock()
    job = harness.job(lock=lock)
    if incremental:
        job = derive_job(harness, DeriveClock(ORIGIN + timedelta(minutes=5)))
        job.dependencies.hierarchy = replace(job.dependencies.hierarchy, lock=lock)

    def pause():
        started.set()
        assert release.wait(5), "test must release synchronous work"

    harness.evolver.before_evolve = pause

    async def run():
        pending = asyncio.create_task(job.run())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            job.request_cancel()
            assert not pending.done(), "cooperative cancellation cannot kill the write thread"
        finally:
            release.set()
        with pytest.raises(JobCancelledError) as caught:
            await pending
        return caught.value.detail

    detail = asyncio.run(run())
    assert detail["created_parent_count"] == "1" and detail["updated_child_count"] == "1"
    assert harness.read(leaf).hierarchy.parent_id

    if incremental:
        assert len(harness.evolver.requests) == 1, "取消后不能进入上层构建"
        assert not job.state.needs_repair, "完整结果已核对，协作取消不应触发损坏闸门"
