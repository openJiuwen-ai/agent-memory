"""E-06 对齐测试——Engine 与后台 Job 必须使用同一套 IndexBuilder / Evolver。

背景（docs/pluginized-assembly-current-issues.md E-06）：Job Spec 装配期曾按
``vector_enabled`` 自行推导默认 IndexBuilder，与 Engine 写入用的可能不是同一
实例——原文由 A 写入、归档却调 B.remove，索引清不掉。修复后：

- Spec 装配期不再解析 index/evolver；缺失注入时 ``get_job`` 显式抛 ValidationError；
- Engine 提交时必传注入（middle 路径传写入用的 index/evolver，evolve 传
  Engine 装配的 evolver），运行时注入优先于 Spec 兜底字段；
- 验收：给 Engine 和 Job Spec 配两套不同 Builder 时，Job 始终用 Engine 的。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJobSpec
from jiuwen_memory.control.jobs_impl.middle_to_long_job import MiddleToLongJobSpec
from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition
from jiuwen_memory.control.types import Channel, JobStatus
from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

pytestmark = pytest.mark.unit

_SCOPE = Scope(user="u1")


# ---- 测试替身 ----


class _RecordingScheduler:
    """记录 submit 入参的 Scheduler 替身（不实际执行 Job）。"""

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


class _RecordingIndex(IndexBuilder):
    """记录 build/remove 的 IndexBuilder 替身（build 交付 Storage）。"""

    def __init__(self, storage=None) -> None:
        self.built: list[MemoryUnit] = []
        self.removed: list[MemoryUnit] = []
        self._storage = storage

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        self.built.extend(units)
        if self._storage is not None:
            for unit in units:
                self._storage.add(unit.scope, [unit])

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        if self._storage is not None:
            for unit in units:
                self._storage.update(unit.scope, [unit])

    def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD) -> None:
        self.removed.extend(units)

    def rebuild(self) -> None:
        return None


class _StubEvolver(Evolver):
    """可用 Evolver 桩——evolve 返回空派生结果（Job 归档路径不依赖派生内容）。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units, mode: EvolveMode) -> EvolveResult:
        return EvolveResult(created_ids=[f"derived-{unit.id}" for unit in units])


class _ContinuityLLM(LLM):
    """连续性检测 LLM 桩——返回固定 JSON 让全部连续（单批）。"""

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        return '{"results":["true"]}'


class _NoopLifecycle(LifecycleManager):
    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope, unit_ids, target) -> None:
        return None

    def supersede(self, scope, unit_id, invalid_at):
        raise NotImplementedError

    def sweep(self) -> list[SweepTransition]:
        return []


# ---- Spec 层：缺失注入显式报错，注入优先于 Spec 兜底 ----


def test_middle_to_long_spec_without_injection_raises() -> None:
    """E-06：Spec 不再自解析 index/evolver——缺注入时 with_scope 显式失败。"""
    storage = CompositeStorage(kv=InMemoryKVStore())
    spec = MiddleToLongJobSpec(
        storage=storage, lifecycle=_NoopLifecycle(), llm=_ContinuityLLM()
    )

    with pytest.raises(ValidationError, match="Evolver"):
        spec.with_scope(_SCOPE, index=_RecordingIndex())
    with pytest.raises(ValidationError, match="IndexBuilder"):
        spec.with_scope(_SCOPE, evolver=_StubEvolver())


def test_middle_to_long_runtime_injection_overrides_spec_fallback() -> None:
    """Engine 注入优先：Spec 持有另一套 Builder/Evolver 时，Job 用注入的。"""
    storage = CompositeStorage(kv=InMemoryKVStore())
    spec_builder = _RecordingIndex()  # Spec 兜底（另一套）
    spec_evolver = _StubEvolver()
    spec = MiddleToLongJobSpec(
        storage=storage,
        lifecycle=_NoopLifecycle(),
        llm=_ContinuityLLM(),
        index=spec_builder,
        evolver=spec_evolver,
    )

    engine_builder = _RecordingIndex()
    engine_evolver = _StubEvolver()
    job = spec.with_scope(
        _SCOPE, index=engine_builder, evolver=engine_evolver, interval=1
    )

    assert job._index is engine_builder  # pylint: disable=protected-access
    assert job._evolver is engine_evolver  # pylint: disable=protected-access
    assert spec_builder.built == [] and spec_builder.removed == []


def test_evolve_spec_without_injection_raises() -> None:
    """E-06：EvolveJobSpec 不再自解析 evolver——缺注入时 with_scope 显式失败。"""
    storage = CompositeStorage(kv=InMemoryKVStore())
    spec = EvolveJobSpec(storage=storage)

    with pytest.raises(ValidationError, match="Evolver"):
        spec.with_scope(_SCOPE, mode=EvolveMode.EXTRACT)


# ---- Engine 层：evolve 必传注入 Engine 的 evolver ----


def _evolve_job_factory() -> JobFactory:
    factory = JobFactory()
    factory.register(
        JobType.EVOLVE,
        EvolveJobSpec(storage=CompositeStorage(kv=InMemoryKVStore())).with_scope,
    )
    return factory


def test_engine_evolve_injects_own_evolver_into_job() -> None:
    """Engine.evolve 提交的 EvolveJob 持有 Engine 装配的同一 Evolver。"""
    evolver = _StubEvolver()
    scheduler = _RecordingScheduler()
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        storage=CompositeStorage(kv=InMemoryKVStore()),
        scheduler=scheduler,
        evolver=evolver,
        lifecycle=None,
        job_factory=_evolve_job_factory(),
    )

    asyncio.run(engine.evolve(_SCOPE, EvolveMode.CONSOLIDATE, Channel.HOT))

    assert len(scheduler.calls) == 1
    job, _ = scheduler.calls[0]
    assert job._evolver is evolver  # pylint: disable=protected-access


def test_engine_evolve_without_evolver_raises() -> None:
    """Engine 未装配 evolver 时 evolve 显式失败——不允许 Job 侧另解析一套。"""
    engine = InMemoryEngine(
        ingestor=None,
        index_builder=None,
        retriever=None,
        storage=CompositeStorage(kv=InMemoryKVStore()),
        scheduler=_RecordingScheduler(),
        evolver=None,
        lifecycle=None,
        job_factory=_evolve_job_factory(),
    )

    with pytest.raises(RuntimeError, match="requires an Evolver"):
        asyncio.run(engine.evolve(_SCOPE, EvolveMode.EXTRACT, Channel.HOT))


# ---- 端到端：双 Builder 下 Job 始终用 Engine 的 ----


def test_middle_job_uses_engine_builder_not_spec_fallback() -> None:
    """E-06 验收：write 由 Builder A 建索引，Job 归档 remove 只落在 A。

    Spec 故意持有另一套 Builder B（模拟修复前的错误装配）——Engine 运行时
    注入必须覆盖它：原文检索索引的写入与移除同源。
    """
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv)
    builder_a = _RecordingIndex(storage)  # Engine 写入用
    builder_b = _RecordingIndex()  # Spec 兜底——绝不该被调到
    evolver = _StubEvolver()
    lifecycle = _NoopLifecycle()
    factory = JobFactory()
    factory.register(
        JobType.MIDDLE_TO_LONG,
        MiddleToLongJobSpec(
            storage=storage,
            lifecycle=lifecycle,
            llm=_ContinuityLLM(),
            index=builder_b,
            evolver=evolver,
        ).with_scope,
    )
    scheduler = _RecordingScheduler()
    engine = InMemoryEngine(
        ingestor=SimpleIngestor(normalizer=PassthroughNormalizer()),
        index_builder=builder_a,
        retriever=None,
        storage=storage,
        scheduler=scheduler,
        evolver=evolver,
        lifecycle=lifecycle,
        classifier=None,
        pipeline=None,
        job_factory=factory,
    )

    asyncio.run(
        engine.write(
            "hello middle",
            _SCOPE,
            system_metadata={"infer": "true", "middle": "true"},
        )
    )

    # 原文经 Engine 的 Builder A 建索引（write middle 路径）
    assert len(builder_a.built) == 1
    assert builder_b.built == []

    # Engine 提交时注入 A——覆盖 Spec 兜底的 B
    assert len(scheduler.calls) == 1
    job, _ = scheduler.calls[0]
    assert job._index is builder_a  # pylint: disable=protected-access

    # 手动执行 Job（绕过 Timer）：归档 remove 只落在 A，B 全程零调用
    result = asyncio.run(job.run())
    assert result.status == JobStatus.SUCCEEDED
    assert len(builder_a.removed) == 1
    assert builder_b.removed == []
    assert builder_b.built == []
