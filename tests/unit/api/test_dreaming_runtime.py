"""真实 Driver → Command → Engine → AsyncTimerScheduler → EvolveJob 回归。"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.type_def import CandidateGroup, CandidateOutcome, FanOutCandidate, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode, EvolveResult
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit
SCOPE = Scope(org="dreaming-test", user="owner")


def _wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("timed out waiting for dreaming runtime")


class _BlockingJob(Job):
    def __init__(self):
        super().__init__(scope=SCOPE)
        self.started = threading.Event()
        self.release = threading.Event()

    async def run(self):
        self.started.set()
        await asyncio.to_thread(self.release.wait, 5)
        return JobInfo(scope=self.scope)


@pytest.fixture
def runtime():
    kernel = _build_kernel(config=Config.from_dict({
        "permission": {"default": "sqlite"},
        "scheduler": {"default": {"target": "async_timer", "params": {"tick_interval": 1}}},
    }))
    try:
        yield kernel
    finally:
        kernel.scheduler.shutdown()
        kernel.api.release_dreaming_lock()
        kernel.ingest_jobs.close(wait=True)


def _register(runtime, candidate=None):
    return runtime.api._dreaming.register(
        scope=SCOPE, actor=SCOPE, mode=EvolveMode.EXTRACT,
        channel=Channel.BACKGROUND, candidate=candidate, interval=1,
    )


def _stop(runtime, reason):
    if reason == "unregister":
        runtime.api._dreaming.unregister(SCOPE, EvolveMode.EXTRACT)
    else:
        registry = runtime.api._dreaming._registry
        registry.release_instance_lock()
        registry._notify_lock_lost()


def _pending_driver(scheduler, parent_id):
    async def snapshot():
        return next((info.id for info in scheduler._jobs.values()
                     if info.detail.get("parent_timer") == parent_id
                     and info.status == JobStatus.PENDING), None)

    return asyncio.run_coroutine_threadsafe(
        snapshot(), scheduler._runner.ensure_loop()
    ).result(timeout=5)


def test_real_tick_executes_evolver_and_returns_string_job_id(runtime):
    called = threading.Event()

    def evolve(units, mode):
        called.set()
        return EvolveResult()

    runtime.api._engine._evolver = SimpleNamespace(evolve=evolve)
    parent_id = _register(runtime)
    assert called.wait(5), "真实定时驱动必须触达 Evolver"
    child_id = runtime.scheduler.status(parent_id).detail["last_job_id"]
    assert isinstance(child_id, str)
    _wait_for(lambda: runtime.scheduler.status(child_id).status == JobStatus.SUCCEEDED)


def test_unregister_cancels_queued_driver_and_reregister_starts_fresh(runtime):
    called = threading.Event()
    runtime.api._engine._evolver = SimpleNamespace(
        evolve=lambda units, mode: (called.set(), EvolveResult())[1],
    )
    blocker = _BlockingJob()
    asyncio.run(runtime.scheduler.submit(blocker, Channel.BACKGROUND))
    try:
        assert blocker.started.wait(5)
        parent_id = _register(runtime)
        driver_id = _wait_for(lambda: _pending_driver(runtime.scheduler, parent_id))
        _stop(runtime, "unregister")
        blocker.release.set()
        _wait_for(lambda: runtime.scheduler.status(driver_id).status == JobStatus.CANCELLED)
        assert not called.is_set(), "已注销的排队驱动不能再提交演进"
        new_id = _register(runtime)
        assert new_id != parent_id
        assert called.wait(5), "新注册不能继承旧生命周期的取消信号"
    finally:
        blocker.release.set()


@pytest.mark.parametrize("reason", ["unregister", "lock_lost"])
def test_stop_cancels_already_queued_evolve_job(runtime, reason):
    called = threading.Event()
    runtime.api._engine._evolver = SimpleNamespace(
        evolve=lambda units, mode: (called.set(), EvolveResult())[1],
    )
    first, second = _BlockingJob(), _BlockingJob()
    asyncio.run(runtime.scheduler.submit(first, Channel.BACKGROUND))
    try:
        assert first.started.wait(5)
        parent_id = _register(runtime)
        _wait_for(lambda: _pending_driver(runtime.scheduler, parent_id))
        # FIFO: first → driver → second → driver 派生的 EvolveJob。
        asyncio.run(runtime.scheduler.submit(second, Channel.BACKGROUND))
        first.release.set()
        assert second.started.wait(5)
        child_id = runtime.scheduler.status(parent_id).detail["last_job_id"]
        assert runtime.scheduler.status(child_id).status == JobStatus.PENDING
        _stop(runtime, reason)
        second.release.set()
        _wait_for(lambda: runtime.scheduler.status(child_id).status == JobStatus.CANCELLED)
        assert not called.is_set(), "派生任务入队后也必须受父任务取消约束"
    finally:
        first.release.set()
        second.release.set()


@pytest.mark.parametrize("reason", ["unregister", "lock_lost"])
def test_stop_during_first_fan_out_bucket_prevents_second_bucket(runtime, reason, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def evolve(units, mode):
        calls.append(mode)
        entered.set()
        assert release.wait(5), "测试必须释放执行中的同步 Evolver"
        return EvolveResult()

    runtime.api._engine._evolver = SimpleNamespace(evolve=evolve)
    buckets = [Scope(org=SCOPE.org, user=SCOPE.user, session=str(i)) for i in range(2)]
    monkeypatch.setattr(runtime.api._dreaming, "_scope_supplier", lambda: buckets)
    parent_id = _register(runtime, FanOutCandidate())
    try:
        assert entered.wait(5)
        child_id = runtime.scheduler.status(parent_id).detail["last_job_id"]
        _stop(runtime, reason)
        release.set()
        _wait_for(lambda: runtime.scheduler.status(child_id).status == JobStatus.CANCELLED)
        assert len(calls) == 1, "当前同步调用结束后不得开始第二个桶"
    finally:
        release.set()


def test_cancel_while_evolver_waits_in_thread_pool(runtime):
    """任务已过主循环检查但尚未取得工作线程时，取消仍阻止同步调用。"""
    occupied, release, resolved = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def occupy_worker():
        occupied.set()
        release.wait(5)

    class Resolver:
        async def resolve(self):
            resolved.set()
            return CandidateOutcome(groups=[CandidateGroup(scope=SCOPE)])

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(occupy_worker)
        assert occupied.wait(5)
        scheduler = runtime.scheduler
        # 长间隔父任务仅用于取消归属，不在测试期间触发。
        parent = _BlockingJob()
        parent.interval = 60
        parent_id = asyncio.run(scheduler.submit(parent, Channel.BACKGROUND))

        async def submit_child():
            asyncio.get_running_loop().set_default_executor(executor)
            child = EvolveJob(
                scope=SCOPE, kv=runtime.kv, resolver=Resolver(),
                evolver=SimpleNamespace(evolve=lambda units, mode: calls.append(mode)),
            )
            child_id = await scheduler.submit(child, Channel.BACKGROUND)
            scheduler.link_child(child_id, parent_id)
            return child_id

        try:
            child_id = asyncio.run_coroutine_threadsafe(
                submit_child(), scheduler._runner.ensure_loop(),
            ).result(timeout=5)
            assert resolved.wait(5)
            scheduler.cancel(parent_id)
            release.set()
            _wait_for(lambda: scheduler.status(child_id).status == JobStatus.CANCELLED)
            assert calls == [], "线程池中尚未开始的 Evolver 必须检查取消信号"
        finally:
            release.set()
