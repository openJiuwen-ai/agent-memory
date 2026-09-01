from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    MemoryTier,
    MemoryUnit,
    Modality,
    RawPayload,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.classifier import Classifier
from jiuwen_memory.construction.evolver import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.cloud_engine import CloudEngine
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJobSpec
from jiuwen_memory.control.jobs_impl.middle_to_long_job import MiddleToLongJobSpec
from jiuwen_memory.control.lifecycle import LifecycleManager
from jiuwen_memory.control.pipeline import MemoryPipeline, PipelineBinding
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import (
    BatchWriteItem,
    Channel,
    DeleteSelector,
    MemoryPatch,
    UpdateMode,
)
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.ingestor import Ingestor
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult, RetrievedItem
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

pytestmark = pytest.mark.unit


class _RecordingIngestor(Ingestor):
    def operator_type(self) -> IngestOperatorType:
        return IngestOperatorType.INGESTOR

    def health(self) -> None:
        return None

    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        units: list[MemoryUnit] = []
        now = datetime.now(timezone.utc)
        for payload in payloads:
            units.append(
                MemoryUnit(
                    id=payload.id,
                    scope=payload.scope,
                    segments=[
                        Segment(
                            content=payload.data.decode("utf-8"),
                            source=payload.modality,
                        )
                    ],
                    temporal=Temporal(
                        t_event=payload.occurred_at or now,
                        t_ingest=now,
                        t_valid=now,
                    ),
                    system_metadata=dict(payload.system_metadata),
                    user_metadata=dict(payload.user_metadata),
                )
            )
        return units


class _RecordingIndexBuilder(IndexBuilder):
    """记录调用并交付 Storage 的替身——IndexBuilder 是记忆写入的唯一入口。"""

    def __init__(self, name: str, storage=None) -> None:
        self.name = name
        self.built: list[str] = []
        self.updated: list[str] = []
        self.removed: list[str] = []
        self._storage = storage

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        self.built.extend(unit.content for unit in units)
        # 遵守契约：mode=RETRIEVAL_ONLY 表示本体已存在、只补建派生索引，不得再写本体。
        if mode is not IndexWriteMode.RETRIEVAL_ONLY and self._storage is not None:
            for unit in units:
                self._storage.add(unit.scope, [unit])

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        self.updated.extend(unit.id for unit in units)
        if self._storage is not None:
            for unit in units:
                self._storage.update(unit.scope, [unit])

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        self.removed.extend(unit.id for unit in units)
        if mode is IndexRemoveMode.HARD and self._storage is not None:
            for unit in units:
                self._storage.delete(unit.scope, [unit.id])

    def rebuild(self) -> None:
        return None


class _RecordingClassifier(Classifier):
    def __init__(self, name: str) -> None:
        self.name = name
        self.classified: list[str] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.CLASSIFIER

    def health(self) -> None:
        return None

    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        for unit in units:
            unit.system_metadata["classified_by"] = self.name
            self.classified.append(unit.content)
        return units


class _RecordingRetriever(Retriever):
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[str] = []
        self.extensions: list[dict] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        self.queries.append(query.extensions.get("message_type", ""))
        self.extensions.append(query.extensions)
        return RetrievalResult(items=[RetrievedItem(unit_id=self.name, content=query.text)])


class _RecordingEvolver(Evolver):
    def __init__(self, name: str, kv: InMemoryKVStore) -> None:
        self.name = name
        self.kv = kv
        self.calls: list[tuple[list[str], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append(([unit.content for unit in units], mode))
        created_ids: list[str] = []
        for unit in units:
            derived = MemoryUnit(
                id=f"{self.name}-derived-{len(created_ids)}",
                scope=unit.scope,
                segments=[Segment(content=f"derived:{unit.content}", source=unit.source)],
                temporal=unit.temporal,
                provenance=[unit.id],
                system_metadata=dict(unit.system_metadata),
                user_metadata=dict(unit.user_metadata),
            )
            self.kv.insert(unit.scope, memory_key(derived.id), dumps(derived))
            created_ids.append(derived.id)
        return EvolveResult(created_ids=created_ids)


class _RecordingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls = []

    def list(self, scope, **kwargs):
        self.list_calls.append((scope, kwargs))
        return super().list(scope, **kwargs)


class _NoopLifecycle(LifecycleManager):
    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope, unit_ids, target) -> None:
        return None

    def supersede(self, scope, unit_id, invalid_at):
        raise AssertionError("not used in these tests")

    def sweep(self):
        return []


class _RecordingScheduler(InProcessScheduler):
    """记录 submit 入参的 Scheduler 替身（不实际执行 Job）。

    继承 InProcessScheduler 保留 status/cancel，但 submit 不调 job.run——
    让测试断言 Job 字段后无副作用。
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Job, Channel]] = []

    async def submit(self, job: Job, channel: Channel) -> str:
        self.calls.append((job, channel))
        return "job-1"


def _build_test_job_factory(
    kv, evolver, lifecycle, index, *, llm=None
) -> JobFactory:
    """构造测试用 JobFactory——注册 MiddleToLongJob + EvolveJob 的 Spec builder。

    与 InMemoryEngine 测试同模式——Spec 装配期固化依赖与业务参数，
    运行时 get_job 补 scope + 运行时参数生成完整 Job 实例。
    """
    from jiuwen_memory.common.base import PluginType
    from jiuwen_memory.common.llm.base import LLM
    from jiuwen_memory.common.type_def.chat import ChatMessage

    class _EchoLLM(LLM):
        def plugin_type(self) -> PluginType:
            return PluginType.LLM

        def health(self) -> None:
            return None

        def chat(self, messages: list[ChatMessage], **options: object) -> str:
            return messages[-1].content if messages else ""

    storage = CompositeStorage(kv=kv)
    factory = JobFactory()
    factory.register(
        JobType.MIDDLE_TO_LONG,
        MiddleToLongJobSpec(
            storage=storage,
            evolver=evolver,
            lifecycle=lifecycle,
            index=index,
            llm=llm or _EchoLLM(),
            max_fetch=100,
            batch_size=10,
            concurrency=1,
        ).with_scope,
    )
    factory.register(
        JobType.EVOLVE,
        EvolveJobSpec(storage=storage, evolver=evolver).with_scope,
    )
    return factory


class _MessageTypePipeline(MemoryPipeline):
    def __init__(self, profiles: dict[str, PipelineBinding]) -> None:
        self.profiles = profiles

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PIPELINE

    def health(self) -> None:
        return None

    def select_for_write(self, units: list[MemoryUnit]) -> PipelineBinding:
        route = units[0].system_metadata.get("message_type", "chat")
        return self.profiles.get(route, self.profiles["chat"])

    def select_for_recall(self, query: RetrievalQuery) -> PipelineBinding:
        route = query.extensions.get("message_type", "chat")
        return self.profiles.get(route, self.profiles["chat"])


def _engine():
    kv = _RecordingKVStore()
    storage = CompositeStorage(kv=kv)
    chat_index = _RecordingIndexBuilder("chat", storage)
    coding_index = _RecordingIndexBuilder("coding", storage)
    chat_classifier = _RecordingClassifier("chat")
    coding_classifier = _RecordingClassifier("coding")
    chat_retriever = _RecordingRetriever("chat")
    coding_retriever = _RecordingRetriever("coding")
    chat_evolver = _RecordingEvolver("chat", kv)
    coding_evolver = _RecordingEvolver("coding", kv)
    profiles = {
        "chat": PipelineBinding(
            name="chat",
            index_builder=chat_index,
            retriever=chat_retriever,
            evolver=chat_evolver,
            classifier=chat_classifier,
        ),
        "coding": PipelineBinding(
            name="coding",
            index_builder=coding_index,
            retriever=coding_retriever,
            evolver=coding_evolver,
            classifier=coding_classifier,
        ),
    }
    return (
        CloudEngine(
            ingestor=_RecordingIngestor(),
            index_builder=chat_index,
            retriever=chat_retriever,
            storage=storage,
            scheduler=InProcessScheduler(),
            evolver=chat_evolver,
            lifecycle=_NoopLifecycle(),
            classifier=chat_classifier,
            pipeline=_MessageTypePipeline(profiles),
            default_message_type="chat",
            default_pipeline_name="chat",
        ),
        {
            "kv": kv,
            "chat_index": chat_index,
            "coding_index": coding_index,
            "chat_classifier": chat_classifier,
            "coding_classifier": coding_classifier,
            "chat_retriever": chat_retriever,
            "coding_retriever": coding_retriever,
            "chat_evolver": chat_evolver,
            "coding_evolver": coding_evolver,
        },
    )


def test_cloud_engine_write_routes_by_message_type_and_stamps_metadata() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "use pytest for this repo",
            scope,
            source=Modality.CODE,
            system_metadata={"message_type": "coding", "memory_type": "procedural"},
        )
    )

    assert records["chat_index"].built == []
    assert records["coding_index"].built == ["use pytest for this repo"]
    assert records["coding_classifier"].classified == ["use pytest for this repo"]
    assert units[0].system_metadata["message_type"] == "coding"
    assert units[0].system_metadata["pipeline"] == "coding"
    assert units[0].system_metadata["classified_by"] == "coding"

    context = asyncio.run(engine.permission_context_for_unit(units[0].id, scope))

    assert context.pipeline == "coding"
    assert context.memory_type == "procedural"
    assert context.metadata["message_type"] == "coding"


def test_cloud_engine_write_defaults_to_chat_message_type() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(engine.write("remember my meeting notes", scope))

    assert records["chat_index"].built == ["remember my meeting notes"]
    assert records["coding_index"].built == []
    assert units[0].system_metadata["message_type"] == "chat"
    assert units[0].system_metadata["pipeline"] == "chat"


def test_cloud_engine_batch_write_preserves_order_and_routes_each_item() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    result = asyncio.run(
        engine.batch_write(
            [
                BatchWriteItem(content="chat note", scope=scope, source=Modality.TEXT),
                BatchWriteItem(
                    content="coding note",
                    scope=scope,
                    source=Modality.CODE,
                    system_metadata={"message_type": "coding"},
                ),
            ]
        )
    )

    assert [outcome.units[0].content for outcome in result.outcomes] == ["chat note", "coding note"]
    assert records["chat_index"].built == ["chat note"]
    assert records["coding_index"].built == ["coding note"]
    assert result.outcomes[1].units[0].system_metadata["pipeline"] == "coding"


def test_cloud_engine_batch_write_collects_unexpected_error_and_skips_after_failure() -> None:
    engine, _ = _engine()
    scope = Scope(org="acme", user="alice")

    async def _raise_unexpected(*_args, **_kwargs):
        raise RuntimeError("unavailable dependency")

    engine.write = _raise_unexpected  # type: ignore[method-assign]
    result = asyncio.run(
        engine.batch_write(
            [BatchWriteItem(content="first", scope=scope), BatchWriteItem(content="second", scope=scope)],
            continue_on_error=False,
        )
    )

    assert [outcome.error_type for outcome in result.outcomes] == ["InternalError", "Skipped"]
    assert result.outcomes[0].error == "unexpected batch write failure"


def test_cloud_engine_recall_routes_by_message_type_extension() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    result = asyncio.run(
        engine.recall(scope, RetrievalQuery(text="testing", extensions={"message_type": "coding"}))
    )

    assert [item.unit_id for item in result.items] == ["coding"]
    assert records["coding_retriever"].queries == ["coding"]
    assert records["chat_retriever"].queries == []


def test_cloud_engine_recall_keeps_runtime_extension_identity() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")
    marker = object()

    result = asyncio.run(
        engine.recall(
            scope,
            RetrievalQuery(text="testing", extensions={"db_query_service": marker}),
        )
    )

    assert result.items
    routed = records["chat_retriever"]
    assert routed.queries == ["chat"]
    assert routed.extensions[0]["db_query_service"] is marker


def test_cloud_engine_list_forwards_query_and_returns_total_count() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", space="coding", user="alice")
    first = asyncio.run(
        engine.write(
            "first alpha memory",
            scope,
            system_metadata={"memory_type": "coding"},
            user_metadata={"project": "alpha"},
        )
    )[0]
    second = asyncio.run(
        engine.write(
            "second alpha memory",
            scope,
            system_metadata={"memory_type": "coding"},
            user_metadata={"project": "alpha"},
        )
    )[0]
    asyncio.run(
        engine.write(
            "beta memory",
            scope,
            system_metadata={"memory_type": "coding"},
            user_metadata={"project": "beta"},
        )
    )
    filters = FilterClause("user_metadata.project", FilterOp.EQ, "alpha")
    extensions = {"vendor_mode": "strict"}

    result = asyncio.run(
        engine.list(
            scope,
            offset=1,
            limit=1,
            memory_types=["coding"],
            filters=filters,
            extensions=extensions,
        )
    )

    assert result.count == 2
    assert len(result.items) == 1
    assert result.items[0].id in {first.id, second.id}
    call_scope, call_options = records["kv"].list_calls[0]
    assert call_scope == scope
    assert call_options["offset"] == 1
    assert call_options["limit"] == 1
    assert call_options["memory_types"] == ["coding"]
    assert call_options["filters"] is filters
    assert call_options["extensions"] is extensions


def test_cloud_engine_infer_uses_profile_evolver_and_returns_derived_units() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "extract coding preference",
            scope,
            system_metadata={"message_type": "coding", "infer": "true"},
        )
    )

    assert records["coding_evolver"].calls == [(["extract coding preference"], EvolveMode.EXTRACT)]
    assert records["chat_evolver"].calls == []
    assert units[0].id == "coding-derived-0"
    assert units[0].content == "derived:extract coding preference"
    assert units[0].system_metadata["pipeline"] == "coding"
    assert units[0].system_metadata["message_type"] == "coding"


def test_cloud_engine_overwrite_moves_unit_between_profile_indexes() -> None:
    engine, records = _engine()
    scope = Scope(org="acme", user="alice")
    units = asyncio.run(engine.write("chat note", scope))

    updated = asyncio.run(
        engine.update(
            units[0].id,
            scope,
            MemoryPatch(
                content="coding note",
                system_metadata={"message_type": "coding"},
                mode=UpdateMode.OVERWRITE,
            ),
        )
    )

    assert updated.id == units[0].id
    assert updated.system_metadata["message_type"] == "coding"
    assert updated.system_metadata["pipeline"] == "coding"
    assert records["chat_index"].removed == [units[0].id]
    assert records["coding_index"].built == ["coding note"]


def test_cloud_engine_delete_rejects_empty_selector() -> None:
    engine, _ = _engine()

    try:
        asyncio.run(engine.delete(DeleteSelector()))
    except ValidationError:
        return
    else:
        raise AssertionError("empty selector should raise ValidationError")


# ---- mem2.0：middle 路径 + evolve 走 JobFactory ----


def _engine_with_job_factory(
    *,
    with_job_factory: bool = True,
):
    """构造带 JobFactory 的 CloudEngine——多 profile binding（chat/coding）。

    注册 MiddleToLongJob + EvolveJob 的 Spec builder，Spec 装配期固化的 evolver
    是 chat_evolver（default）。CloudEngine _write_middle_path 内会覆盖
    job._evolver / job._index 为 binding 选的——这是测试要验证的关键点。
    """
    kv = InMemoryKVStore()
    storage = CompositeStorage(kv=kv)
    chat_index = _RecordingIndexBuilder("chat", storage)
    coding_index = _RecordingIndexBuilder("coding", storage)
    chat_evolver = _RecordingEvolver("chat", kv)
    coding_evolver = _RecordingEvolver("coding", kv)
    lifecycle = _NoopLifecycle()
    scheduler = _RecordingScheduler()
    profiles = {
        "chat": PipelineBinding(
            name="chat",
            index_builder=chat_index,
            retriever=_RecordingRetriever("chat"),
            evolver=chat_evolver,
            classifier=_RecordingClassifier("chat"),
        ),
        "coding": PipelineBinding(
            name="coding",
            index_builder=coding_index,
            retriever=_RecordingRetriever("coding"),
            evolver=coding_evolver,
            classifier=_RecordingClassifier("coding"),
        ),
    }
    factory = _build_test_job_factory(kv, chat_evolver, lifecycle, chat_index) if with_job_factory else None
    engine = CloudEngine(
        ingestor=_RecordingIngestor(),
        index_builder=chat_index,
        retriever=_RecordingRetriever("chat"),
        storage=storage,
        scheduler=scheduler,
        evolver=chat_evolver,
        lifecycle=lifecycle,
        classifier=_RecordingClassifier("chat"),
        pipeline=_MessageTypePipeline(profiles),
        default_message_type="chat",
        default_pipeline_name="chat",
        job_factory=factory,
    )
    return engine, scheduler, {
        "kv": kv,
        "chat_index": chat_index,
        "coding_index": coding_index,
        "chat_evolver": chat_evolver,
        "coding_evolver": coding_evolver,
        "lifecycle": lifecycle,
    }


def test_cloud_engine_write_middle_submits_middle_to_long_job() -> None:
    """infer=true + middle=true → _write_middle_path：提交 MiddleToLongJob 到 scheduler。

    验证：
    - scheduler 收到 MiddleToLongJob（type 名匹配）；
    - job.scope == scope；
    - job.interval == 50（metadata 未传 middle_interval，回退 Spec 装配期默认）；
    - 原文落盘 tier=WORKING + metadata.middle=true；
    - 立即建索引（index.build 已调）；
    - job._evolver / job._index 被 binding 选的覆盖（coding profile）。
    """
    engine, scheduler, records = _engine_with_job_factory()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "alice likes tea",
            scope,
            system_metadata={"message_type": "coding", "infer": "true", "middle": "true"},
        )
    )

    # scheduler 收到 1 个 MiddleToLongJob
    assert len(scheduler.calls) == 1
    job, channel = scheduler.calls[0]
    assert channel == Channel.BACKGROUND
    assert job.scope == scope
    assert job.interval == 50
    # 原文落盘 + tier=WORKING + metadata.middle=true
    assert len(units) >= 1
    persisted = loads(records["kv"].get(scope, memory_key(units[0].id)))
    assert persisted.tier == MemoryTier.WORKING
    assert persisted.system_metadata.get("middle") == "true"
    # 立即建索引（coding_index 收到 build）
    assert records["coding_index"].built == ["alice likes tea"]
    # job._evolver / job._index 被 binding 的覆盖——
    # Spec 装配期固化的 evolver 是 chat_evolver（_engine_with_job_factory 内），
    # 但 binding 选了 coding profile，故 job._evolver 应为 coding_evolver。
    assert job._evolver is records["coding_evolver"]  # pylint: disable=protected-access
    assert job._index is records["coding_index"]  # pylint: disable=protected-access


def test_cloud_engine_write_middle_raises_when_job_factory_is_none() -> None:
    """_write_middle_path 无 job_factory → RuntimeError（middle 路径必须装配 JobFactory）。"""
    engine, scheduler, _ = _engine_with_job_factory(with_job_factory=False)
    scope = Scope(org="acme", user="alice")

    with pytest.raises(RuntimeError, match="middle path requires job_factory"):
        asyncio.run(
            engine.write(
                "x",
                scope,
                system_metadata={"message_type": "chat", "infer": "true", "middle": "true"},
            )
        )


@pytest.mark.parametrize(
    "middle_interval",
    ["abc", "0", "-1"],
)
def test_cloud_engine_write_middle_invalid_interval_raises_before_persist(
    middle_interval: str,
) -> None:
    """非法 middle_interval 在落盘前 fail fast，不残留 KV/索引/Job（Refs #182）。"""
    engine, scheduler, records = _engine_with_job_factory()
    scope = Scope(org="acme", user="alice")

    with pytest.raises(ValidationError, match=r"middle_interval"):
        asyncio.run(
            engine.write(
                "oscar likes pottery",
                scope,
                system_metadata={
                    "message_type": "chat",
                    "infer": "true",
                    "middle": "true",
                    "middle_interval": middle_interval,
                },
            )
        )

    assert scheduler.calls == []
    assert records["chat_index"].built == []
    assert records["kv"].list(scope, limit=100).entries == []


def test_cloud_engine_procedural_takes_precedence_over_middle() -> None:
    """procedural=true 优先——即使 middle=true，也走 procedural 路径（不提交 MiddleToLongJob）。

    与 InMemoryEngine 同模式——procedural > infer 互斥逻辑里 procedural 优先，
    middle 是 infer 下的二级开关，procedural=true 时 middle 标记被忽略。
    """
    engine, scheduler, records = _engine_with_job_factory()
    scope = Scope(org="acme", user="alice")

    units = asyncio.run(
        engine.write(
            "alice likes tea",
            scope,
            system_metadata={"message_type": "chat", "procedural": "true", "middle": "true"},
        )
    )

    # procedural 路径：不提交 MiddleToLongJob
    assert scheduler.calls == []
    # 派生结果而非原文
    assert all(u.id.startswith("chat-derived-") for u in units)


def test_cloud_engine_evolve_submits_evolve_job_via_job_factory() -> None:
    """evolve 经 JobFactory 取 EvolveJob(mode=mode) 提交——修复原 submit(scope, mode, channel) bug。

    Scheduler 已统一为 submit(job, channel)——原 CloudEngine.evolve 调
    submit(scope, mode, channel) 必报 TypeError。重构后走 JobFactory 统一创建路径。
    """
    engine, scheduler, _ = _engine_with_job_factory()
    scope = Scope(org="acme", user="alice")

    job_id = asyncio.run(engine.evolve(scope, EvolveMode.CONSOLIDATE, Channel.HOT))

    assert job_id == "job-1"
    assert len(scheduler.calls) == 1
    job, channel = scheduler.calls[0]
    assert channel == Channel.HOT
    assert job.scope == scope
    assert job.interval == 0  # EvolveJob 是一次性任务
    assert job._mode == EvolveMode.CONSOLIDATE  # mode 经构造参数流入  # pylint: disable=protected-access


def test_cloud_engine_evolve_raises_when_job_factory_is_none() -> None:
    """evolve 无 job_factory → RuntimeError（原 submit(scope, mode, channel) bug 修复路径）。"""
    engine, scheduler, _ = _engine_with_job_factory(with_job_factory=False)
    scope = Scope(org="acme", user="alice")

    with pytest.raises(RuntimeError, match="evolve requires job_factory"):
        asyncio.run(engine.evolve(scope, EvolveMode.EXTRACT, Channel.HOT))
