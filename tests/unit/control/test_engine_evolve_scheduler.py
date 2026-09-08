from __future__ import annotations

import asyncio

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.config import Config
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJobSpec
from jiuwen_memory.control.types import BatchWriteItem, Channel, JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

_TEST_KEY_HEX = "00" * 32


class RaisingEvolver(Evolver):
    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units, mode: EvolveMode) -> EvolveResult:
        raise AssertionError("Engine.evolve should delegate execution to Scheduler")


class RecordingScheduler:
    """记录 submit 调用入参的 Scheduler 替身（不实际执行 Job）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Job, Channel]] = []

    async def submit(self, job: Job, channel: Channel) -> str:
        self.calls.append((job, channel))
        return "job-1"

    @staticmethod
    def status(job_id: str):
        ...

    @staticmethod
    def cancel(job_id: str) -> None:
        ...


def _build_test_job_factory(evolver) -> JobFactory:
    """构造测试用 JobFactory——注册 EvolveJob 的 Spec builder。

    Engine.evolve 经 JobFactory.get_job(JobType.EVOLVE, scope, mode=mode)
    取 EvolveJob 实例——Engine 不再直接 new EvolveJob（统一 Job 创建路径）。
    """
    factory = JobFactory()
    factory.register(
        JobType.EVOLVE,
        EvolveJobSpec(kv=InMemoryKVStore(), evolver=evolver).with_scope,
    )
    return factory


def test_engine_evolve_only_submits_scheduler_job() -> None:
    """Engine.evolve 经 JobFactory 取 EvolveJob(mode=mode) 提交，不实际执行 evolver。"""
    scope = Scope(user="u1")
    scheduler = RecordingScheduler()
    evolver = RaisingEvolver()
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=scheduler,
        evolver=evolver,
        lifecycle=None,
        job_factory=_build_test_job_factory(evolver),
    )

    job_id = asyncio.run(engine.evolve(scope, EvolveMode.CONSOLIDATE, Channel.HOT))

    assert job_id == "job-1"
    assert len(scheduler.calls) == 1
    job, channel = scheduler.calls[0]
    assert channel == Channel.HOT
    assert job.scope == scope
    assert job.interval == 0
    # mode 经 EvolveJob 构造参数流入——不该由 Scheduler 看到或硬编码
    assert job._mode == EvolveMode.CONSOLIDATE  # pylint: disable=protected-access


def test_in_memory_batch_write_collects_unexpected_error_and_continues() -> None:
    scope = Scope(user="u1")
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=None,
        evolver=None,
        lifecycle=None,
    )
    attempted: list[str] = []

    async def _write(content, *_args, **_kwargs):
        attempted.append(content)
        if content == "bad":
            raise RuntimeError("unavailable dependency")
        return []

    engine.write = _write  # type: ignore[method-assign]
    result = asyncio.run(
        engine.batch_write(
            [
                BatchWriteItem(content="bad", scope=scope),
                BatchWriteItem(content="good", scope=scope),
            ]
        )
    )

    assert attempted == ["bad", "good"]
    assert result.outcomes[0].error_type == "InternalError"
    assert not result.outcomes[1].error


def test_api_evolve_returns_completed_scheduler_job_with_evolve_result_detail() -> None:
    # 显式覆盖 scheduler=in_process——本测试验证 evolve 语义（同步 SUCCEEDED），
    # 不验证 AsyncTimerScheduler 的异步调度行为（后者由阶段 5 集成测试覆盖）。
    # AsyncTimerScheduler 需事件循环驱动，submit 后不立即完成，与同步断言不兼容。
    config = Config.from_dict(
        {
            "scheduler": {"default": {"target": "in_process", "params": {}}},
            "security": {
                "default": {"target": "local", "params": {"key_hex": _TEST_KEY_HEX}}
            },
        }
    )
    kernel = build_kernel(config=config)
    scope = Scope(user="u1")
    kernel.api.add("Alice likes tea", scope, security=legacy_request_context(scope))

    job_id = kernel.api.evolve(scope, EvolveMode.EXTRACT, security=legacy_request_context(scope))

    job = kernel.api.job_status(job_id, security=legacy_request_context(scope))
    assert job.status == JobStatus.SUCCEEDED
    assert job.detail["created_ids"] is not None
    assert job.detail["updated_ids"] == ""
    assert job.detail["superseded_ids"] == ""
    assert job.detail["forgotten_ids"] == ""
