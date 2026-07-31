"""最小实现：:class:`~control.scheduler.Scheduler`。

同步调度：任务提交后在当前进程内立即执行。第一阶段执行体仍为 no-op，
但保留完整任务状态流，返回任务 id 供 ``status`` / ``cancel`` 查询。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from common.errors import NotFoundError
from common.log import get_logger
from common.type_def import MEMORY_KEY_PREFIX, Scope
from common.type_def.memory_codec import loads
from construction import EvolveMode, Evolver, EvolveResult
from construction.evolver import EvolverProducer
from control.base import ControlOperatorType
from control.scheduler import Scheduler, SchedulerProducer
from control.types import Channel, JobInfo, JobStatus
from storage.kv import KvProducer, KVStore

logger = get_logger(__name__)


class InProcessScheduler(Scheduler):
    """同步调度：任务提交后立即执行（演进在第一阶段里为 no-op）。"""

    def __init__(self, kv: KVStore | None = None, evolver: Evolver | None = None) -> None:
        self._jobs: dict[str, JobInfo] = {}
        self._kv = kv
        self._evolver = evolver

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SCHEDULER

    def health(self) -> None:
        return None

    def submit(self, scope: Scope, mode: EvolveMode, channel: Channel) -> str:
        job_id = str(uuid.uuid4())
        job = JobInfo(
            id=job_id,
            channel=channel,
            mode=mode.value,
            scope=scope,
            status=JobStatus.PENDING,
        )
        self._jobs[job_id] = job
        logger.info(
            "Scheduler.submit: job_id=%s scope=%s mode=%s channel=%s",
            job_id,
            scope,
            mode.value,
            channel.value,
        )
        job.status = JobStatus.RUNNING
        job.detail["started_at"] = self._now_iso()
        try:
            self._execute_task(job)
        except Exception as exc:  # scheduler records task failure as job state
            job.status = JobStatus.FAILED
            job.detail["error_type"] = type(exc).__name__
            job.detail["error"] = str(exc)
            logger.warning(
                "Scheduler.failed: job_id=%s mode=%s error_type=%s error=%s",
                job_id,
                job.mode,
                type(exc).__name__,
                exc,
            )
        else:
            job.status = JobStatus.SUCCEEDED
            logger.info("Scheduler.succeeded: job_id=%s mode=%s", job_id, job.mode)
        finally:
            job.detail["finished_at"] = self._now_iso()
        return job_id

    def status(self, job_id: str) -> JobInfo:
        if job_id not in self._jobs:
            logger.warning("Scheduler.status missing job: job_id=%s", job_id)
            raise NotFoundError("job", job_id)
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None and job.status == JobStatus.PENDING:
            job.status = JobStatus.CANCELLED
            logger.info("Scheduler.cancelled: job_id=%s", job_id)
        elif job is None:
            logger.debug("Scheduler.cancel ignored missing job: job_id=%s", job_id)
        else:
            logger.debug(
                "Scheduler.cancel ignored non-pending job: job_id=%s status=%s",
                job_id,
                job.status.value,
            )

    def _execute_task(self, job: JobInfo) -> None:
        if self._kv is None or self._evolver is None:
            logger.debug("Scheduler.execute skipped: job_id=%s reason=no_executor", job.id)
            return
        units = []
        for _, raw in self._kv.scan(job.scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is not None:
                units.append(unit)
        logger.info(
            "Scheduler.execute: job_id=%s mode=%s units=%d",
            job.id,
            job.mode,
            len(units),
        )
        result = self._evolver.evolve(units, EvolveMode(job.mode))
        self._record_evolve_result(job, result)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _record_evolve_result(self, job: JobInfo, result: EvolveResult) -> None:
        job.detail["created_ids"] = ",".join(result.created_ids)
        job.detail["updated_ids"] = ",".join(result.updated_ids)
        job.detail["superseded_ids"] = ",".join(result.superseded_ids)
        job.detail["forgotten_ids"] = ",".join(result.forgotten_ids)


# -- 注册到 SchedulerProducer（实现自注册，新增无需改 producer/装配入口） -------- #




@SchedulerProducer.register("in_process")
def _build(config):
    # 真源 kv 与构建链 evolver 与引擎侧共享同一实例（按节点名共享）；
    # 缺了执行器后台 EXTRACT/CONSOLIDATE 只会空转（标 SUCCEEDED 却不落结果）。
    return InProcessScheduler(
        KvProducer.dep(config, default="memory"),
        EvolverProducer.dep(config, default="orchestrating"),
    )
