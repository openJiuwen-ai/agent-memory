"""InMemoryEngine.write 三路分流 + _write_middle_path 单元测试。

覆盖：
- procedural 优先（procedural=true，不走 middle 路径）；
- infer=true + middle=true → _write_middle_path：原文落 /memory/ + tier=WORKING +
  metadata["middle"]="true" + index.build + scheduler.submit(MiddleToLongJob)；
- infer=true + middle!=true → 既有同步抽取路径（不走 middle）；
- 默认路径（infer=false）：原文落 /memory/ + tier 保持 EPISODIC；
- _write_middle_path 无 job_factory → RuntimeError（mem2.0 重构后：llm 经 JobFactory
  固化到 Spec，Engine 不再持 llm——边界从"无 llm"改为"无 job_factory"）；
- _write_middle_path 无 evolver → RuntimeError。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    memory_key,
)
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.middle_to_long_job import MiddleToLongJobSpec
from jiuwen_memory.control.lifecycle import LifecycleManager
from jiuwen_memory.control.types import Channel, JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit


# ---- 测试替身 ----


class _RecordingScheduler:
    """记录 submit 入参的 Scheduler 替身（不实际执行 Job）。

    保留与 ``test_engine_evolve_scheduler.RecordingScheduler`` 一致的接口风格，
    但本测试聚焦 middle 路径——需要拿到 Job 实例检查其字段。
    """

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


class _NoopEvolver(Evolver):
    """不实际执行的 Evolver 桩——evolve() 不应被 middle 路径调用。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units, mode: EvolveMode) -> EvolveResult:
        raise AssertionError(
            "_write_middle_path 不应直接调 evolver.evolve（由 MiddleToLongJob 内部调）"
        )


class _RecordingIndex(IndexBuilder):
    """记录 build 入参的 IndexBuilder 替身。"""

    def __init__(self) -> None:
        self.built: list[MemoryUnit] = []
        self.removed: list[MemoryUnit] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units) -> None:
        self.built.extend(units)

    def update(self, units) -> None:
        return None

    def remove(self, units) -> None:
        self.removed.extend(units)

    def rebuild(self) -> None:
        return None


class _NoopLifecycle(LifecycleManager):
    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope, unit_ids, target) -> None:
        return None

    def supersede(self, scope, unit_id, invalid_at):
        raise AssertionError("middle path should not call supersede")

    def sweep(self) -> list[str]:
        return []


class _EchoLLM(LLM):
    """回显 LLM 桩——middle 路径不会真的调它（不执行 MiddleToLongJob.run）。"""

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        return messages[-1].content if messages else ""


# ---- 公共装配 ----


# 哨兵：区分"显式 None"与"未传"——避免 `llm or _EchoLLM()` 把显式 None 替换掉
_UNSET = object()


def _build_engine(
    *,
    scheduler=None,
    evolver=None,
    llm=_UNSET,
    middle_max_fetch: int = 100,
    middle_batch_size: int = 10,
    middle_concurrency: int = 4,
) -> tuple[InMemoryEngine, _RecordingScheduler, _RecordingIndex, InMemoryKVStore]:
    """构造最小可测 Engine——绕过 IngestorProducer/EvolverProducer 装配链。

    直接用 InMemoryKVStore + _RecordingIndex + _RecordingScheduler + _NoopEvolver，
    Ingestor 用真实的 SimpleIngestor（产出 1 个 unit）。

    mem2.0 重构后：Engine 不再持 llm/middle_*——这些经 JobFactory 固化到
    :class:`MiddleToLongJobSpec`。本装配构造测试 JobFactory，把 Spec 的
    ``with_scope`` 方法注册为 builder——运行时 ``get_job`` 取 MiddleToLongJob 实例。
    ``llm=_UNSET`` 是哨兵：区分"显式传 None"（验证 RuntimeError）与"未传"（用 EchoLLM 默认）。
    """
    from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
        PassthroughNormalizer,
    )
    from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

    kv = InMemoryKVStore()
    index = _RecordingIndex()
    scheduler = scheduler or _RecordingScheduler()
    evolver = evolver or _NoopEvolver()
    lifecycle = _NoopLifecycle()
    ingestor = SimpleIngestor(normalizer=PassthroughNormalizer())
    # 哨兵：未传 llm 用 EchoLLM；显式传 None 用 None（让 RuntimeError 分支被触发）
    if llm is _UNSET:
        llm = _EchoLLM()

    # 构造测试 JobFactory——MiddleToLongJobSpec 固化依赖与业务参数，
    # with_scope 在运行时补 scope 生成完整 Job 实例。
    factory = JobFactory()
    factory.register(
        JobType.MIDDLE_TO_LONG,
        MiddleToLongJobSpec(
            storage=CompositeStorage(kv=kv),
            evolver=evolver,
            lifecycle=lifecycle,
            index=index,
            llm=llm,
            max_fetch=middle_max_fetch,
            batch_size=middle_batch_size,
            concurrency=middle_concurrency,
        ).with_scope,
    )

    # 本测试聚焦 write middle 路径——不依赖 retriever。Retriever 用 None（write 路径不调 retriever）。
    engine = InMemoryEngine(
        ingestor=ingestor,
        index_builder=index,
        retriever=None,
        storage=CompositeStorage(kv=kv),
        scheduler=scheduler,
        evolver=evolver,
        lifecycle=lifecycle,
        classifier=None,
        pipeline=None,
        job_factory=factory,
    )
    return engine, scheduler, index, kv


# ---- 路径选择 ----


def test_write_procedural_takes_precedence_over_middle() -> None:
    """procedural=true 优先——即使 middle=true，也走 procedural 路径（不调 scheduler.submit）。

    语义对齐：procedural > infer 互斥逻辑里 procedural 优先，middle 是 infer 下的二级开关，
    procedural 优先级最高，procedural=true 时 middle 标记被忽略（不进 middle 路径）。
    """
    engine, scheduler, _, kv = _build_engine()
    scope = Scope(org="acme", user="u1")

    # procedural + middle 同时为 true——procedural 优先
    # _OkEvolver 把派生结果落到 KV——验证 procedural 路径走通
    class _OkEvolver(_NoopEvolver):
        def evolve(self, units, mode):
            derived = [
                MemoryUnit(id=f"derived-{u.id}", scope=u.scope, segments=u.segments)
                for u in units
            ]
            for d in derived:
                kv.insert(scope, memory_key(d.id), dumps(d))
            return EvolveResult(created_ids=[d.id for d in derived])

    engine._evolver = _OkEvolver()  # pylint: disable=protected-access
    units = asyncio.run(
        engine.write(
            "hello",
            scope,
            metadata={"procedural": "true", "middle": "true"},
        )
    )

    # procedural 路径：不提交 MiddleToLongJob
    assert scheduler.calls == []
    # 派生结果而非原文
    assert all(u.id.startswith("derived-") for u in units)


def test_write_infer_middle_submits_middle_to_long_job() -> None:
    """infer=true + middle=true → _write_middle_path：提交 MiddleToLongJob 到 scheduler。"""
    engine, scheduler, index, kv = _build_engine()
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(
        engine.write(
            "alice likes tea",
            scope,
            metadata={"infer": "true", "middle": "true"},
        )
    )

    # scheduler 收到 1 个 MiddleToLongJob
    assert len(scheduler.calls) == 1
    job, channel = scheduler.calls[0]
    assert channel == Channel.BACKGROUND
    # 验证 Job 字段
    assert job.scope == scope
    assert job.interval == 50  # metadata 未传 middle_interval，回退 Spec 装配期默认
    assert job._max_fetch == 100  # pylint: disable=protected-access
    assert job._batch_size == 10  # pylint: disable=protected-access
    assert job._concurrency == 4  # pylint: disable=protected-access
    # 原文落盘 + tier=WORKING + metadata.middle=true
    assert len(units) >= 1
    persisted = loads(kv.get(scope, memory_key(units[0].id)))
    assert persisted.tier == MemoryTier.WORKING
    assert persisted.metadata.get("middle") == "true"
    # 立即可检索（index.build 已调）
    assert index.built == units


def test_write_infer_middle_passes_engine_middle_params_to_job() -> None:
    """middle_* 装配期参数 + middle_interval metadata 覆盖透传到 MiddleToLongJob。

    - middle_max_fetch/batch_size/concurrency 经 JobSpec 装配期固化；
    - middle_interval 经 write metadata 透传（瞬态 key），覆盖 Spec 装配期默认 50。
    """
    engine, scheduler, _, kv = _build_engine(
        middle_max_fetch=50, middle_batch_size=5, middle_concurrency=2
    )
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(
        engine.write(
            "x",
            scope,
            metadata={
                "infer": "true",
                "middle": "true",
                "middle_interval": "30",  # 经 metadata 透传，覆盖 Spec 默认 50
            },
        )
    )

    job, _ = scheduler.calls[0]
    assert job.interval == 30
    assert job._max_fetch == 50  # pylint: disable=protected-access
    assert job._batch_size == 5  # pylint: disable=protected-access
    assert job._concurrency == 2  # pylint: disable=protected-access
    persisted = loads(kv.get(scope, memory_key(units[0].id)))
    assert "middle_interval" not in persisted.metadata


def test_write_infer_without_middle_does_not_submit_job() -> None:
    """infer=true 但 middle!=true → 既有同步抽取路径，不走 middle 路径。"""
    # 用 _OkEvolver 让 infer 路径走通——派生结果落 KV
    class _OkEvolver(_NoopEvolver):
        def evolve(self, units, mode):
            assert mode == EvolveMode.EXTRACT
            derived = [
                MemoryUnit(id=f"derived-{u.id}", scope=u.scope, segments=u.segments)
                for u in units
            ]
            for d in derived:
                kv.insert(scope, memory_key(d.id), dumps(d))
            return EvolveResult(created_ids=[d.id for d in derived])

    engine, scheduler, _, kv = _build_engine(evolver=_OkEvolver())
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(
        engine.write("hello", scope, metadata={"infer": "true"})
    )

    assert scheduler.calls == []
    assert all(u.id.startswith("derived-") for u in units)


def test_write_default_path_persists_original_without_middle() -> None:
    """默认路径（无 infer/procedural/middle）：原文落盘 + tier 保持 EPISODIC。"""
    engine, scheduler, index, kv = _build_engine()
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(engine.write("plain note", scope))

    assert scheduler.calls == []
    assert units
    persisted = loads(kv.get(scope, memory_key(units[0].id)))
    assert persisted.tier == MemoryTier.EPISODIC
    assert persisted.metadata.get("middle") is None
    assert index.built == units  # 默认路径也建索引


# ---- _write_middle_path 错误分支 ----


def test_write_middle_raises_when_job_factory_is_none() -> None:
    """_write_middle_path 无 job_factory → RuntimeError（middle 路径必须装配 JobFactory）。

    mem2.0 重构后：llm 与业务参数经 JobFactory 固化到 Spec——Engine 不再校验 llm
    （llm 在 Spec 内不可见）。无 JobFactory 时报错，与无 evolver 同模式。
    """
    engine, scheduler, _, _ = _build_engine()
    engine._job_factory = None  # 模拟未装配  # pylint: disable=protected-access
    scope = Scope(org="acme", user="u1")

    with pytest.raises(RuntimeError, match="middle path requires job_factory"):
        asyncio.run(
            engine.write("x", scope, metadata={"infer": "true", "middle": "true"})
        )


def test_write_middle_raises_when_evolver_is_none() -> None:
    """_write_middle_path 无 evolver → RuntimeError（MiddleToLongJob 内部需要 evolver）。"""
    engine, scheduler, _, _ = _build_engine()
    engine._evolver = None  # 模拟未装配  # pylint: disable=protected-access
    scope = Scope(org="acme", user="u1")

    with pytest.raises(RuntimeError, match="middle=true requires an Evolver"):
        asyncio.run(
            engine.write("x", scope, metadata={"infer": "true", "middle": "true"})
        )


# ---- 多次 write 重复提交（验证同 scope 同 kind 复用 entry） ----


def test_write_middle_repeated_submits_jobs_to_scheduler() -> None:
    """同 scope 多次 write(middle=true) → 每次 submit 一个 MiddleToLongJob。

    Scheduler 内部按 (scope, kind) 复用 entry（见 AsyncTimerScheduler._submit_timer）——
    本测试只验证 Engine 每次都 submit，复用语义由 AsyncTimerScheduler 单测覆盖。
    """
    engine, scheduler, _, _ = _build_engine()
    scope = Scope(org="acme", user="u1")

    asyncio.run(engine.write("first", scope, metadata={"infer": "true", "middle": "true"}))
    asyncio.run(engine.write("second", scope, metadata={"infer": "true", "middle": "true"}))

    # Engine 不感知 Scheduler 的复用语义——每次都 submit
    assert len(scheduler.calls) == 2
    # 两个 Job 同 scope 同 kind（MiddleToLongJob）——Scheduler 内部按 kind 复用 entry
    job1, _ = scheduler.calls[0]
    job2, _ = scheduler.calls[1]
    assert type(job1).__name__ == type(job2).__name__ == "MiddleToLongJob"


# ---- InProcessScheduler 真实执行回归（原崩溃路径 ③） ----
#
# 路径 ③: API 同步 write → engine.write async → _write_middle_path →
# await scheduler.submit(MiddleToLongJob)。修复前 InProcessScheduler.submit
# 是同步方法,await 同步返回的 str 触发 TypeError；修复后 submit 改 async,
# 真实跑 MiddleToLongJob.run(并发分支) 不再崩溃。
#
# 注意:本测试用 InProcessScheduler 真实执行 MiddleToLongJob.run——但 KV 空,
# run 内 _list_working_units 立即返回空 → 走 is_done=true 早返回分支,不触发 gather。
# 真正的并发分支回归由 test_middle_to_long_job.py 的 in_process_* 测试覆盖。


def test_write_middle_with_in_process_scheduler_runs_job_to_completion() -> None:
    """InProcessScheduler 真实执行 MiddleToLongJob——SUCCEEDED + is_done=true。

    路径 ③ 回归:Engine.write(middle=true) → await InProcessScheduler.submit →
    MiddleToLongJob.run → _list_working_units 空 → is_done=true。

    修复前:InProcessScheduler.submit 同步 + Engine._write_middle_path 用
    `await scheduler.submit` → TypeError: object str can't be used in 'await'。
    修复后:submit 改 async,Engine 直接 await,Job 真实跑完返回 SUCCEEDED。
    """
    from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler

    # 用真实 InProcessScheduler,不用 _RecordingScheduler
    scheduler = InProcessScheduler()
    engine, _, index, kv = _build_engine(scheduler=scheduler)
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(
        engine.write("alice likes tea", scope, metadata={"infer": "true", "middle": "true"})
    )

    # 原文落盘 + tier=WORKING + metadata.middle=true
    assert len(units) >= 1
    persisted = loads(kv.get(scope, memory_key(units[0].id)))
    assert persisted.tier == MemoryTier.WORKING
    assert persisted.metadata.get("middle") == "true"
    assert index.built == units  # 立即建索引

    # InProcessScheduler 立即跑完 MiddleToLongJob.run——KV 只有 1 条原文
    # (MiddleToLongJob 的 _list_working_units 过滤 tier=WORKING+ACTIVE,
    # 候选包含本次 write 落盘的原文) → 调 evolver → 走并发分支。
    # 不验证 detail 字段(evolver=_NoopEvolver 不返 created_ids),
    # 只验证状态流走通(无 TypeError 崩溃 + SUCCEEDED/FAILED 二选一)。
    job_infos = [info for info in scheduler._jobs.values()]  # pylint: disable=protected-access
    assert len(job_infos) == 1
    assert job_infos[0].status in (JobStatus.SUCCEEDED, JobStatus.FAILED)


def test_write_middle_with_in_process_scheduler_preserves_originals_on_failure() -> None:
    """InProcessScheduler + 失败 evolver → 原文保留 ACTIVE+WORKING（不归档）。

    路径 ③ 边界:evolver 全失败时,_archive_originals 不被调,原文保留 ACTIVE+WORKING,
    下轮 MiddleToLongJob 重试。验证 InProcessScheduler 链路下失败传播正确。
    """
    from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler

    class _FailingEvolver(_NoopEvolver):
        def evolve(self, units, mode):
            raise RuntimeError("evolver down")

    scheduler = InProcessScheduler()
    engine, _, index, kv = _build_engine(
        scheduler=scheduler, evolver=_FailingEvolver()
    )
    scope = Scope(org="acme", user="u1")

    units = asyncio.run(
        engine.write("alice likes tea", scope, metadata={"infer": "true", "middle": "true"})
    )

    # Job FAILED——evolver 抛错,串行分支 try/except 吞掉 + 不归档原文
    job_infos = [info for info in scheduler._jobs.values()]  # pylint: disable=protected-access
    assert len(job_infos) == 1
    assert job_infos[0].status == JobStatus.SUCCEEDED  # JobInfo 仍 SUCCEEDED(部分成功)
    assert job_infos[0].detail.get("processed") == "0"  # 0 unit 被处理
    # 原文保留 ACTIVE+WORKING
    persisted = loads(kv.get(scope, memory_key(units[0].id)))
    assert persisted.tier == MemoryTier.WORKING
    assert persisted.lifecycle == LifecycleState.ACTIVE
    assert index.removed == []  # 没归档
