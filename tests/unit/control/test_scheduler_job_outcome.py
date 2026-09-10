# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""两个 Scheduler 必须保留 Job 的真实终态，不能把业务失败改报成功。"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import AsyncTimerScheduler
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus
from tests.unit.control.scheduler_outcome_fixtures import (
    JobOutcome,
    OutcomeJob,
    finish_background_tasks,
    finish_job,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("scheduler_type", [InProcessScheduler, AsyncTimerScheduler])
@pytest.mark.parametrize(
    "terminal_status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
)
def test_scheduler_preserves_returned_terminal_status_and_detail(
    scheduler_type: type[Scheduler], terminal_status: JobStatus,
) -> None:
    """业务终态与 detail 原样保留，调度元数据仍由 Scheduler 持有。"""
    scheduler = scheduler_type()
    job_scope = Scope(org="org", space="space", user="user", session="session")
    returned_detail = {"complete": "false", "repair": "leaf-parent mismatch"}
    job = OutcomeJob(
        scope=job_scope,
        outcome=JobOutcome(status=terminal_status, detail=returned_detail),
    )

    info = asyncio.run(finish_job(scheduler, job))

    assert info.status is terminal_status
    assert info.detail["complete"] == "false"
    assert info.detail["repair"] == "leaf-parent mismatch"
    assert info.id != "job-returned-id"
    assert info.scope == job_scope
    assert info.channel is Channel.HOT
    assert info.mode == "OutcomeJob"
    assert job.calls == [job_scope]
    assert returned_detail == {"complete": "false", "repair": "leaf-parent mismatch"}
    assert (
        datetime.fromisoformat(info.detail["started_at"])
        <= datetime.fromisoformat(info.detail["finished_at"])
    )


@pytest.mark.parametrize("scheduler_type", [InProcessScheduler, AsyncTimerScheduler])
@pytest.mark.parametrize("unfinished_status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_scheduler_rejects_nonterminal_result(
    scheduler_type: type[Scheduler], unfinished_status: JobStatus,
) -> None:
    """Job.run 返回后没有执行者，非终态必须失败并保留业务诊断。"""
    scheduler = scheduler_type()
    job = OutcomeJob(
        outcome=JobOutcome(status=unfinished_status, detail={"repair": "still required"})
    )

    info = asyncio.run(finish_job(scheduler, job))

    assert info.status is JobStatus.FAILED
    assert info.detail["repair"] == "still required"
    assert info.detail["error_type"] == "ValueError"
    assert "must return a terminal status" in info.detail["error"]
    assert unfinished_status.value in info.detail["error"]
    assert info.detail["finished_at"]


@pytest.mark.parametrize("scheduler_type", [InProcessScheduler, AsyncTimerScheduler])
def test_scheduler_preserves_business_failure_diagnostics(
    scheduler_type: type[Scheduler],
) -> None:
    """显式 FAILED 的业务错误不能替换成调度器错误。"""
    scheduler = scheduler_type()
    failure_detail = {
        "error_type": "HierarchyPartialFailure",
        "error": "retrieval index update failed",
        "repair": "rebuild retrieval indexes",
    }
    job = OutcomeJob(outcome=JobOutcome(status=JobStatus.FAILED, detail=failure_detail))

    info = asyncio.run(finish_job(scheduler, job))

    assert info.status is JobStatus.FAILED
    for detail_key, detail_value in failure_detail.items():
        assert info.detail[detail_key] == detail_value


@pytest.mark.parametrize("scheduler_type", [InProcessScheduler, AsyncTimerScheduler])
@pytest.mark.parametrize("exception_type", [RuntimeError, ValueError])
def test_scheduler_still_records_raised_exceptions(
    scheduler_type: type[Scheduler], exception_type: type[Exception],
) -> None:
    """异常路径继续生成 FAILED、异常类型及完成时间。"""
    scheduler = scheduler_type()
    job = OutcomeJob(outcome=JobOutcome(exception=exception_type("job failed")))

    info = asyncio.run(finish_job(scheduler, job))

    assert info.status is JobStatus.FAILED
    assert info.detail["error_type"] == exception_type.__name__
    assert info.detail["error"] == "job failed"
    assert info.detail["started_at"]
    assert info.detail["finished_at"]


@pytest.mark.parametrize("scheduler_type", [InProcessScheduler, AsyncTimerScheduler])
def test_scheduler_still_records_execution_cancellation(
    scheduler_type: type[Scheduler], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务抛 CancelledError 时继续遵守 asyncio 取消协议并记录取消。"""
    scheduler = scheduler_type()
    job = OutcomeJob(outcome=JobOutcome(exception=asyncio.CancelledError()))
    monkeypatch.setattr("uuid.uuid4", lambda: "cancelled-job")

    async def run_cancelled_job() -> JobInfo:
        async with asyncio.timeout(3):
            if scheduler_type is InProcessScheduler:
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.submit(job, Channel.HOT)
            else:
                await scheduler.submit(job, Channel.HOT)
                await job.completed.wait()
            await finish_background_tasks()
        return scheduler.status("cancelled-job")

    info = asyncio.run(run_cancelled_job())

    assert info.status is JobStatus.CANCELLED
    assert info.detail["cancelled_at"]
    assert info.detail["finished_at"]
    assert len(job.calls) == 1


@pytest.mark.parametrize(
    "terminal_status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
)
def test_timer_completion_preserves_final_instance_status(terminal_status: JobStatus) -> None:
    """is_done 停止周期后，周期声明也保留最后实例的真实终态。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    job = OutcomeJob(
        interval=1,
        outcome=JobOutcome(
            status=terminal_status,
            detail={"is_done": "true", "reason": "explicit final outcome"},
        ),
    )

    info = asyncio.run(finish_job(scheduler, job))

    assert info.status is terminal_status
    assert info.detail["is_done"] == "true"
    assert info.detail["reason"] == "explicit final outcome"
    assert info.detail["finished_at"]
    assert len(job.calls) == 1


def test_timer_keeps_running_after_failed_instance_without_is_done() -> None:
    """单次失败不隐式停止周期；下一轮显式 is_done 才结束。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    job = OutcomeJob(interval=1, outcome=JobOutcome(status=JobStatus.FAILED))

    async def run_periodic_job() -> tuple[JobStatus, JobInfo]:
        async with asyncio.timeout(5):
            timer_id = await scheduler.submit(job, Channel.BACKGROUND)
            await job.completed.wait()
            after_failure = scheduler.status(timer_id).status
            job.completed.clear()
            job.outcome.status = JobStatus.SUCCEEDED
            job.outcome.detail["is_done"] = "true"
            await job.completed.wait()
            await finish_background_tasks()
        return after_failure, scheduler.status(timer_id)

    running_status, final_info = asyncio.run(run_periodic_job())

    assert running_status is JobStatus.RUNNING
    assert final_info.status is JobStatus.SUCCEEDED
    assert final_info.detail["is_done"] == "true"
    assert len(job.calls) == 2
