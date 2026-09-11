# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scheduler 终态测试替身：只通过公开 Job / Scheduler 接口观察结果。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus


@dataclass
class JobOutcome:
    """一次 Job 执行的预设结果。"""

    status: JobStatus = JobStatus.SUCCEEDED
    detail: dict[str, str] = field(default_factory=dict)
    exception: BaseException | None = None


@dataclass
class OutcomeJob(Job):
    """发出完成事件的 Job，方便测试等待调度器退出而不访问内部 Task。"""

    outcome: JobOutcome = field(default_factory=JobOutcome)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[Scope] = field(default_factory=list)

    async def run(self) -> JobInfo:
        self.calls.append(self.scope)
        try:
            if self.outcome.exception is not None:
                raise self.outcome.exception
            return JobInfo(
                id="job-returned-id",
                channel=Channel.BACKGROUND,
                mode="job-returned-mode",
                scope=Scope(user="job-returned-user"),
                status=self.outcome.status,
                detail=dict(self.outcome.detail),
            )
        finally:
            self.completed.set()


async def finish_job(scheduler: Scheduler, job: OutcomeJob) -> JobInfo:
    """等待一次性 Job 及本测试事件循环内的后台 Task 正常退出。"""
    async with asyncio.timeout(3):
        submitted_id = await scheduler.submit(job, Channel.HOT)
        await job.completed.wait()
        await finish_background_tasks()
    return scheduler.status(submitted_id)


async def finish_background_tasks() -> None:
    """通过 asyncio 公开接口收齐当前测试创建的后台任务。"""
    active_task = asyncio.current_task()
    other_tasks = [task for task in asyncio.all_tasks() if task is not active_task]
    await asyncio.gather(*other_tasks, return_exceptions=True)
