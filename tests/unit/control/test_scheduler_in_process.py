"""InProcessScheduler 同步调度：``submit`` 入口的状态流与错误处理。

迁移自旧 ``submit(scope, mode, channel)`` 接口——本类现在只接收 Job 调
``job.run()``，task 内容由 Job 定义（``EvolveJob`` 或测试用的 fake Job）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


class _StubJob(Job):
    """最简 fake Job——记录 run 是否被调、可选注入返回的 JobInfo 或异常。

    用于聚焦 Scheduler 的状态流测试，不依赖 EvolveJob 的真实拉数据逻辑。
    """

    def __init__(
        self,
        scope: Scope,
        *,
        detail: dict[str, str] | None = None,
        exc: Exception | None = None,
    ) -> None:
        super().__init__(scope=scope, interval=0)
        self._detail = detail or {}
        self._exc = exc
        self.run_called = False

    async def run(self) -> JobInfo:
        self.run_called = True
        if self._exc is not None:
            raise self._exc
        return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED, detail=self._detail)


class RecordingEvolver(Evolver):
    def __init__(self) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        return EvolveResult(
            created_ids=["created-1"],
            updated_ids=["updated-1"],
            superseded_ids=["old-1"],
            forgotten_ids=["forgotten-1"],
        )


def test_submit_runs_to_success_with_sync_state_flow() -> None:
    """submit → PENDING→RUNNING→SUCCEEDED，detail 含 started_at/finished_at。"""
    scheduler = InProcessScheduler()
    scope = Scope(user="u1")
    job = _StubJob(scope, detail={"created_ids": "x"})

    job_id = asyncio.run(scheduler.submit(job, Channel.HOT))

    info = scheduler.status(job_id)
    assert job.run_called
    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["started_at"]
    assert info.detail["finished_at"]
    assert (
        datetime.fromisoformat(info.detail["started_at"])
        <= datetime.fromisoformat(info.detail["finished_at"])
    )
    assert info.detail["created_ids"] == "x"


def test_submit_records_failed_job_when_run_raises() -> None:
    """run 抛异常 → FAILED + detail 含 error_type/error。"""
    scheduler = InProcessScheduler()
    scope = Scope(user="u1")
    job = _StubJob(scope, exc=RuntimeError("scheduler boom"))

    job_id = asyncio.run(scheduler.submit(job, Channel.BACKGROUND))

    info = scheduler.status(job_id)
    assert info.status == JobStatus.FAILED
    assert info.detail["error_type"] == "RuntimeError"
    assert info.detail["error"] == "scheduler boom"
    assert info.detail["started_at"]
    assert info.detail["finished_at"]


def test_submit_runs_evolve_job_with_units_from_scope() -> None:
    """真 EvolveJob：list scope 全部 MemoryUnit + 调 evolver.evolve + 记 detail。

    验证 EvolveJob + InProcessScheduler 端到端：mode 由构造参数传入。
    """
    scope = Scope(user="u1")
    other_scope = Scope(user="u2")
    kv = InMemoryKVStore()
    kv.insert(
        scope,
        memory_key("unit-1"),
        dumps(MemoryUnit(id="unit-1", scope=scope, segments=[Segment(content="one")])),
    )
    kv.insert(
        scope,
        memory_key("unit-2"),
        dumps(MemoryUnit(id="unit-2", scope=scope, segments=[Segment(content="two")])),
    )
    kv.insert(
        other_scope,
        memory_key("other-unit"),
        dumps(
            MemoryUnit(
                id="other-unit", scope=other_scope, segments=[Segment(content="other")]
            )
        ),
    )
    evolver = RecordingEvolver()
    scheduler = InProcessScheduler()

    job = EvolveJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        mode=EvolveMode.ASSOCIATE,
    )
    job_id = asyncio.run(scheduler.submit(job, Channel.BACKGROUND))

    info = scheduler.status(job_id)
    assert info.status == JobStatus.SUCCEEDED
    assert len(evolver.calls) == 1
    units, mode = evolver.calls[0]
    assert mode == EvolveMode.ASSOCIATE
    assert {u.id for u in units} == {"unit-1", "unit-2"}
    assert info.detail["created_ids"] == "created-1"
    assert info.detail["updated_ids"] == "updated-1"
    assert info.detail["superseded_ids"] == "old-1"
    assert info.detail["forgotten_ids"] == "forgotten-1"


def test_cancel_is_idempotent_and_does_not_change_completed_jobs() -> None:
    """cancel 幂等——已完成任务不被改状态，缺失 job 不抛错。

    InProcessScheduler 同步执行：submit 后已 SUCCEEDED，cancel 无效。
    """
    scheduler = InProcessScheduler()
    scope = Scope(user="u1")
    job_id = asyncio.run(scheduler.submit(_StubJob(scope), Channel.BACKGROUND))

    scheduler.cancel(job_id)
    scheduler.cancel("missing-job")

    assert scheduler.status(job_id).status == JobStatus.SUCCEEDED
