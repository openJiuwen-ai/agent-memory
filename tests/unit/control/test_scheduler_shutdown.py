# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shutdown stops periodic work and joins only this scheduler's resources."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import AsyncTimerScheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit


class _Job(Job):
    def __init__(self, *, interval=0, block=False, fail=False):
        super().__init__(Scope(user="u1"), interval=interval)
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs = []
        self.block = block
        self.fail = fail

    async def run(self):
        self.started.set()
        self.runs.append(1)
        if self.block:
            assert await asyncio.to_thread(self.release.wait, 3)
        if self.fail:
            raise ValueError("job failed")
        return JobInfo(scope=self.scope)


def _submit(scheduler, job):
    return asyncio.run(scheduler.submit(job, Channel.BACKGROUND))


def test_close_stops_periodic_work_and_rejects_restarts() -> None:
    scheduler = AsyncTimerScheduler(tick_interval=1)
    job = _Job(interval=1)
    timer_id = _submit(scheduler, job)
    try:
        assert job.started.wait(3)
        scheduler.close()
        assert not scheduler._runner._thread.is_alive()
        assert scheduler._runner._loop.is_closed()
        assert scheduler.status(timer_id).status == JobStatus.CANCELLED
        with pytest.raises(BackendError, match="closed"):
            _submit(scheduler, job)
        with pytest.raises(BackendError, match="closed"):
            scheduler.validate(job)
        scheduler.cancel(timer_id)
        scheduler.close()
    finally:
        scheduler.close()


@pytest.mark.parametrize("fail", [False, True])
def test_nowait_finishes_running_job_but_never_starts_queued_job(fail) -> None:
    scheduler = AsyncTimerScheduler(tick_interval=1)
    running, queued = _Job(block=True, fail=fail), _Job()
    running_id = _submit(scheduler, running)
    assert running.started.wait(3)
    queued_id = _submit(scheduler, queued)
    timer_id = _submit(scheduler, _Job(interval=1))
    try:
        scheduler.close(wait=False)
        assert scheduler._runner._thread.is_alive()
        running.release.set()
        scheduler.close(wait=True)
        assert scheduler.status(running_id).status == (
            JobStatus.FAILED if fail else JobStatus.SUCCEEDED
        )
        assert scheduler.status(queued_id).status == JobStatus.CANCELLED
        assert scheduler.status(timer_id).status == JobStatus.CANCELLED
        assert not queued.started.is_set()
        assert not scheduler._runner._thread.is_alive()
    finally:
        running.release.set()
        scheduler.close()


def test_wait_blocks_for_running_job_and_concurrent_closes_are_idempotent() -> None:
    scheduler = AsyncTimerScheduler()
    job = _Job(block=True)
    _submit(scheduler, job)
    assert job.started.wait(3)
    with ThreadPoolExecutor(max_workers=2) as callers:
        closes = [callers.submit(scheduler.close) for _ in range(2)]
        try:
            assert not job.release.wait(0.05)
            assert not any(future.done() for future in closes)
        finally:
            job.release.set()
        for future in closes:
            future.result(timeout=3)
    assert not scheduler._runner._thread.is_alive()


def test_closing_one_scheduler_does_not_stop_another() -> None:
    first, second = AsyncTimerScheduler(), AsyncTimerScheduler()
    try:
        _submit(first, _Job())
        _submit(second, _Job())
        first.close()
        job = _Job()
        _submit(second, job)
        assert job.started.wait(3)
        assert second._runner._thread.is_alive()
    finally:
        first.close()
        second.close()


def test_close_before_submit_is_terminal() -> None:
    scheduler = AsyncTimerScheduler()
    scheduler.close()
    with pytest.raises(BackendError, match="closed"):
        _submit(scheduler, _Job())
    scheduler.cancel("missing")
    scheduler.close()
    assert not scheduler._runner._thread.is_alive()


def test_blocking_close_from_job_is_rejected_without_deadlock() -> None:
    scheduler = AsyncTimerScheduler()

    async def from_loop():
        with pytest.raises(RuntimeError, match="scheduler loop"):
            scheduler.close()

    try:
        scheduler._runner.run(from_loop(), timeout=3)
        _submit(scheduler, _Job())
    finally:
        scheduler.close()


def test_submit_racing_close_never_leaves_pending_jobs() -> None:
    scheduler = AsyncTimerScheduler()
    gate = threading.Barrier(9)

    def submit():
        gate.wait(timeout=3)
        try:
            return _submit(scheduler, _Job())
        except BackendError as exc:
            assert "closed" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=8) as callers:
        attempts = [callers.submit(submit) for _ in range(8)]
        gate.wait(timeout=3)
        scheduler.close()
        for attempt in attempts:
            job_id = attempt.result(timeout=3)
            if job_id is not None:
                assert scheduler.status(job_id).status in {
                    JobStatus.CANCELLED,
                    JobStatus.SUCCEEDED,
                }
    assert not scheduler._runner._thread.is_alive()
