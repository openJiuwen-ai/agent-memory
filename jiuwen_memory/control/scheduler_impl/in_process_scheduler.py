"""最小实现 Scheduler——同步调度：任务提交后在当前进程内立即执行。

task 内容由 :class:`~control.jobs.Job` 定义。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, Scope
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.evolver import EvolverProducer
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler import Scheduler, SchedulerProducer
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

logger = get_logger(__name__)


class InProcessScheduler(Scheduler):
    """同步调度：任务提交后立即执行。"""

    def __init__(self) -> None:
        """初始化 InProcessScheduler。"""
        self._jobs: dict[str, JobInfo] = {}

    def operator_type(self) -> ControlOperatorType:
        """返回当前算子类型。

        Returns:
            返回 ControlOperatorType。
        """
        return ControlOperatorType.SCHEDULER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    async def submit(self, job: Job, channel: Channel) -> str:
        """执行 `submit` 操作。

        Args:
            job: 参数 job（Job）。
            channel: 参数 channel（Channel）。

        Returns:
            返回 str。
        """
        job_id = str(uuid.uuid4())
        info = JobInfo(
            id=job_id,
            channel=channel,
            mode=type(job).__name__,
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
        """执行 `status` 操作。

        Args:
            job_id: 参数 job_id（str）。

        Returns:
            返回 JobInfo。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        if job_id not in self._jobs:
            logger.warning("InProcessScheduler.status missing job: job_id=%s", job_id)
            raise NotFoundError("job", job_id)
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        """执行 `cancel` 操作。

        Args:
            job_id: 参数 job_id（str）。
        """
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
        """执行 `now_iso` 操作。

        Returns:
            返回 str。
        """
        return datetime.now(timezone.utc).isoformat()


# -- 注册到 SchedulerProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@SchedulerProducer.register("in_process")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return InProcessScheduler()
