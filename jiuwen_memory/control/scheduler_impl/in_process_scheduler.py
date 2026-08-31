# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现 Scheduler——同步调度：任务提交后在当前进程内立即执行。

task 内容由 :class:`~control.jobs.Job` 定义。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler import Scheduler, SchedulerProducer
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

logger = get_logger(__name__)


class InProcessScheduler(Scheduler):
    """同步调度：任务提交后立即执行。"""

    def __init__(self) -> None:
        self._jobs: dict[str, JobInfo] = {}

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SCHEDULER

    def health(self) -> None:
        return None

    async def submit(self, job: Job, channel: Channel) -> str:
        job_id = str(uuid.uuid4())
        info = JobInfo(
            id=job_id,
            channel=channel,
            # 优先取演进模式：鉴权点按它决定任务入口要哪个动作。无演进模式的任务
            # 回落任务类名，保持该字段对既有读取方非空。
            mode=job.mode or type(job).__name__,
            scope=job.scope,
            status=JobStatus.PENDING,
        )
        self._jobs[job_id] = info
        logger.info(
            "InProcessScheduler.submit: job_id=%s scope=%s kind=%s channel=%s",
            job_id,
            job.scope,
            type(job).__name__,
            channel.value,
        )
        info.status = JobStatus.RUNNING
        info.detail["started_at"] = self._now_iso()
        try:
            result = await job.run()
            for k, v in result.detail.items():
                info.detail[k] = v
            info.status = JobStatus.SUCCEEDED
            logger.info(
                "InProcessScheduler.succeeded: job_id=%s kind=%s scope=%s",
                job_id, type(job).__name__, job.scope,
            )
        except asyncio.CancelledError:
            # 事件循环关闭 / 主动 cancel Task——把状态 + 日志打全再重新 raise，
            # 不能吞，否则破坏 asyncio cancel 协议。
            info.status = JobStatus.CANCELLED
            info.detail["cancelled_at"] = self._now_iso()
            logger.info(
                "InProcessScheduler.cancelled_by_loop_shutdown: job_id=%s kind=%s scope=%s",
                job_id, type(job).__name__, job.scope,
            )
            raise
        except Exception as exc:
            info.status = JobStatus.FAILED
            info.detail["error_type"] = type(exc).__name__
            info.detail["error"] = str(exc)
            logger.warning(
                "InProcessScheduler.failed: job_id=%s kind=%s error_type=%s error=%s",
                job_id,
                type(job).__name__,
                type(exc).__name__,
                exc,
            )
        finally:
            info.detail["finished_at"] = self._now_iso()
        return job_id

    def status(self, job_id: str) -> JobInfo:
        if job_id not in self._jobs:
            logger.warning("InProcessScheduler.status missing job: job_id=%s", job_id)
            raise NotFoundError("job", job_id)
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None and job.status == JobStatus.PENDING:
            job.status = JobStatus.CANCELLED
            logger.info("InProcessScheduler.cancelled: job_id=%s", job_id)
        elif job is None:
            logger.debug(
                "InProcessScheduler.cancel ignored missing job: job_id=%s", job_id
            )
        else:
            logger.debug(
                "InProcessScheduler.cancel ignored non-pending job: job_id=%s status=%s",
                job_id,
                job.status.value,
            )

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


# -- 注册到 SchedulerProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@SchedulerProducer.register("in_process")
def _build(config):
    return InProcessScheduler()
