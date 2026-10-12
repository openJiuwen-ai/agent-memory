# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime shutdown reaches the schedulers used by the assembled kernel."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.types import Channel, JobInfo

pytestmark = pytest.mark.unit


class _PeriodicJob(Job):
    async def run(self) -> JobInfo:
        return JobInfo(scope=self.scope)


@pytest.mark.parametrize("engine_target", ["in_memory", "cloud"])
@pytest.mark.parametrize("inline_scheduler", [False, True])
def test_runtime_close_stops_periodic_scheduler(engine_target, inline_scheduler) -> None:
    timer = {"target": "async_timer", "params": {"tick_interval": 1}}
    config = {
        "scheduler": {"default": timer},
        "engine": {
            "default": {
                "target": engine_target,
                "params": {"scheduler": "default"},
            }
        },
    }
    if inline_scheduler:
        config["engine"]["default"]["params"]["scheduler"] = timer
    runtime = assemble_runtime(config=config)
    schedulers = {runtime.api._scheduler, runtime.api._engine._scheduler}
    assert len(schedulers) == (2 if inline_scheduler else 1)
    runners = []
    try:
        for scheduler in schedulers:
            asyncio.run(
                scheduler.submit(_PeriodicJob(Scope(user="u1"), interval=1), Channel.BACKGROUND)
            )
            runners.append(scheduler._runner)
        runtime.close()
        assert all(not runner._thread.is_alive() for runner in runners)
        assert all(runner._loop.is_closed() for runner in runners)
        runtime.close()
    finally:
        runtime.close()


def test_runtime_close_keeps_another_kernel_running() -> None:
    config = {"scheduler": {"default": {"target": "async_timer", "params": {}}}}
    first = assemble_runtime(config=config)
    second = assemble_runtime(config=config)
    first_scheduler = first.api._scheduler
    second_scheduler = second.api._scheduler
    assert first_scheduler is not second_scheduler
    try:
        for scheduler in (first_scheduler, second_scheduler):
            asyncio.run(
                scheduler.submit(_PeriodicJob(Scope(user="u1"), interval=10), Channel.BACKGROUND)
            )
        first.close()
        assert not first_scheduler._runner._thread.is_alive()
        assert second_scheduler._runner._thread.is_alive()
        asyncio.run(
            second_scheduler.submit(_PeriodicJob(Scope(user="u2"), interval=10), Channel.BACKGROUND)
        )
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("failing_component", ["_ingest_jobs", "_engine"])
def test_runtime_closes_root_scheduler_even_if_other_cleanup_fails(
    monkeypatch, failing_component
) -> None:
    runtime = assemble_runtime(
        config={
            "scheduler": {"default": {"target": "async_timer", "params": {}}},
        }
    )
    scheduler = runtime.api._scheduler
    asyncio.run(scheduler.submit(_PeriodicJob(Scope(user="u1"), interval=10), Channel.BACKGROUND))

    def fail(*, wait=True):
        raise ValueError("cleanup failed")

    monkeypatch.setattr(getattr(runtime, failing_component), "close", fail)
    with pytest.raises(ValueError, match="cleanup failed"):
        runtime.close()
    assert not scheduler._runner._thread.is_alive()


@pytest.mark.parametrize("wait", [False, True])
def test_runtime_releases_dreaming_lock_only_after_running_jobs_finish(monkeypatch, wait):
    runtime = assemble_runtime(
        config={"scheduler": {"default": {"target": "async_timer", "params": {}}}}
    )
    scheduler = runtime.api._scheduler
    started = threading.Event()
    finish = threading.Event()
    closing = threading.Event()
    released = []

    class BlockingJob(Job):
        async def run(self):
            started.set()
            await asyncio.to_thread(finish.wait, 5)
            return JobInfo(scope=self.scope)

    original_close = scheduler.close

    def observed_close(*, wait=True):
        closing.set()
        original_close(wait=wait)

    def release_lock():
        assert not scheduler._runner._thread.is_alive()
        assert scheduler._runner._loop.is_closed()
        released.append(True)

    monkeypatch.setattr(scheduler, "close", observed_close)
    monkeypatch.setattr(runtime.api, "release_dreaming_lock", release_lock)
    asyncio.run(scheduler.submit(BlockingJob(Scope(user="owner")), Channel.BACKGROUND))
    assert started.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        close_result = executor.submit(runtime.close, wait=wait)
        try:
            assert closing.wait(2)
            assert released == []
            if wait:
                assert not close_result.done()
            else:
                close_result.result(timeout=2)
        finally:
            finish.set()
            close_result.result(timeout=5)
            runtime.close()
    assert released == [True]
