"""#235：实例关闭后停调度、回收线程且不影响其他实例。"""

# pylint: disable=protected-access  # 白盒检查队列、循环和线程回收，限定于本测试模块

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import AsyncTimerScheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit


class _GatedJob(Job):
    def __init__(self, *, interval: int = 0, fail: bool = False) -> None:
        super().__init__(scope=Scope(user="shutdown-test"), interval=interval)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[int] = []
        self.fail = fail

    async def run(self) -> JobInfo:
        self.calls.append(1)
        self.started.set()
        if self.fail:
            raise RuntimeError("backend unavailable")
        await asyncio.to_thread(self._wait)
        self.check_cancelled()
        return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED)

    def _wait(self) -> None:
        assert self.release.wait(10), "test must release running job"


def _submit(scheduler: AsyncTimerScheduler, job: Job) -> str:
    return asyncio.run(scheduler.submit(job, Channel.BACKGROUND))


@pytest.mark.parametrize("wait", [True, False])
def test_inflight_job_that_finishes_normally_remains_succeeded(wait: bool) -> None:
    scheduler = AsyncTimerScheduler()

    class CompletingJob(_GatedJob):
        async def run(self) -> JobInfo:
            self.started.set()
            await asyncio.to_thread(self._wait)
            # 当前操作已完成，无后续步骤需要因取消跳过。
            return JobInfo(
                scope=self.scope, status=JobStatus.SUCCEEDED, detail={"created_ids": "done"}
            )

    job = CompletingJob()
    try:
        job_id = _submit(scheduler, job)
        assert job.started.wait(3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = threading.Event()

            def close() -> None:
                closing.set()
                scheduler.shutdown(wait=wait)

            future = pool.submit(close)
            try:
                assert closing.wait(3)
                if wait:
                    assert not future.done(), "wait=True must wait for the in-flight operation"
                else:
                    future.result(timeout=3)
            finally:
                job.release.set()
            future.result(timeout=5)
        scheduler.shutdown()
        info = scheduler.status(job_id)
        assert info.status == JobStatus.SUCCEEDED, "normal completion is not cancellation"
        assert info.detail["created_ids"] == "done", "completed side effects must remain visible"
    finally:
        job.release.set()
        scheduler.shutdown()


def test_shutdown_cancels_queue_and_waits_for_running_work() -> None:
    scheduler = AsyncTimerScheduler(tick_interval=1)
    running, queued = _GatedJob(), _GatedJob()
    try:
        running_id = _submit(scheduler, running)
        assert running.started.wait(3)
        queued_id = _submit(scheduler, queued)
        scheduler.shutdown(wait=False)
        with pytest.raises(RuntimeError, match="shut down"):
            _submit(scheduler, _GatedJob())
        assert scheduler._runner._thread.is_alive()
        assert scheduler.status(running_id).status == JobStatus.RUNNING, (
            "status during shutdown must not wait for the running job to finish"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = threading.Event()

            def wait_for_close() -> None:
                waiting.set()
                scheduler.shutdown()

            future = pool.submit(wait_for_close)
            try:
                assert waiting.wait(3)
                assert not future.done(), "shutdown must wait for the running call"
            finally:
                running.release.set()
            future.result(timeout=5)
        assert scheduler.status(running_id).status == JobStatus.CANCELLED
        assert scheduler.status(queued_id).status == JobStatus.CANCELLED
        assert not queued.calls
        assert not scheduler._runner._thread.is_alive()
        assert scheduler._runner._loop.is_closed()
        scheduler.cancel(queued_id)
        scheduler.shutdown()
        assert not scheduler._runner._thread.is_alive(), "history/cancel must not restart loop"
    finally:
        running.release.set()
        scheduler.shutdown()


def test_failed_timer_stops_and_other_instance_remains_usable() -> None:
    first, second = AsyncTimerScheduler(tick_interval=1), AsyncTimerScheduler(tick_interval=1)
    failing, healthy = _GatedJob(interval=1, fail=True), _GatedJob()
    healthy.release.set()
    try:
        timer_id = _submit(first, failing)
        assert failing.started.wait(4)
        first.shutdown()
        assert first.status(timer_id).status == JobStatus.CANCELLED
        assert len(failing.calls) == 1
        assert not first._runner._thread.is_alive()
        job_id = _submit(second, healthy)
        second._runner.run(asyncio.sleep(0))
        assert healthy.started.wait(3)
        assert second._runner._thread.is_alive()
        assert second.status(job_id).status in {JobStatus.RUNNING, JobStatus.SUCCEEDED}
    finally:
        first.shutdown()
        second.shutdown()


def test_submit_shutdown_race_leaves_no_live_tasks() -> None:
    scheduler = AsyncTimerScheduler(tick_interval=1)
    barrier = threading.Barrier(9)

    def submit() -> str | None:
        barrier.wait(timeout=5)
        job = _GatedJob()
        job.release.set()
        try:
            return _submit(scheduler, job)
        except RuntimeError as exc:
            assert "shut down" in str(exc)
            return None

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(submit) for _ in range(8)]
            barrier.wait(timeout=5)
            scheduler.shutdown()
            ids = [future.result(timeout=5) for future in futures]
        for job_id in ids:
            if job_id is not None:
                assert scheduler.status(job_id).status in {
                    JobStatus.SUCCEEDED, JobStatus.CANCELLED,
                }
        assert not scheduler._live_jobs
        assert not scheduler._background_tasks
        assert not scheduler._runner.started or not scheduler._runner._thread.is_alive()
    finally:
        scheduler.shutdown()


def test_shutdown_from_private_loop_is_nonblocking_only() -> None:
    scheduler = AsyncTimerScheduler()

    async def close_on_loop() -> None:
        with pytest.raises(RuntimeError, match="own loop"):
            scheduler.shutdown()
        scheduler.shutdown(wait=False)

    scheduler._runner.run(close_on_loop(), timeout=3)
    scheduler.shutdown()
    assert not scheduler._runner._thread.is_alive()


def test_shutdown_without_submit_does_not_start_thread() -> None:
    scheduler = AsyncTimerScheduler()
    scheduler.shutdown(wait=False)
    scheduler.shutdown()
    scheduler.cancel("unknown")
    with pytest.raises(NotFoundError):
        scheduler.status("unknown")
    assert not scheduler._runner.started
