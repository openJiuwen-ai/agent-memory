# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentEngine 文档模式读路径 + lifecycle 绕过单测。

AgentEngine 默认装配文档模式（真源 md + 影子索引，KV 不持有 MemoryUnit）。
修复前 AgentEngine 照搬 InMemoryEngine 的 KV 直读路径，文档模式下 list/get 读空、
update SUPERSEDE / delete FORGET/ARCHIVE 走 KVLifecycleManager 读空 KV 必
NotFoundError / 静默失效。修复后读取经 DomainStore 分流到影子索引、lifecycle 改写
在 engine 侧绕过 KVLifecycleManager 直接落 shadow（对齐 CloudEngine）。

本文件覆盖：

- write→list/get/``_list_units``/``scopes`` 经 DomainStore（shadow）非空
- update SUPERSEDE 文档模式不抛 NotFoundError + 新版落同 md 文件（coords 反推）
- delete FORGET/ARCHIVE 文档模式 lifecycle 改 + 真源一致
- evolve 传 domain_store 给 resolver（候选非空，非走空 KV）
- 非文档模式回归：list/get 仍走 KV（DomainStore 非文档分支）

装配参考 ``test_cloud_engine.py`` 风格——文档模式真源用真实 SqliteDocumentShadowIndex
+ LocalMarkdownStore + CompositeDomainStore(write_document=True)，IndexBuilder 用
``DocumentIndexBuilder``（全委托 domain_store.add/update/delete 的薄编排层）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MD_FILENAME_KEY,
    LifecycleState,
    MemoryUnit,
    RawPayload,
    RetrievalPipeline,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder_impl.document_index_builder import (
    DocumentIndexBuilder,
)
from jiuwen_memory.construction.index_builder_impl.forward_index_builder import (
    ForwardIndexBuilder,
)
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.agent_engine import AgentEngine
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJobSpec
from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import (
    InProcessScheduler,
)
from jiuwen_memory.control.types import (
    Channel,
    DeleteMode,
    DeleteSelector,
    MemoryPatch,
    UpdateMode,
)
from jiuwen_memory.ingest.base import IngestOperatorType
from jiuwen_memory.ingest.ingestor import Ingestor
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.markdown_impl.local_markdown_store import (
    LocalMarkdownStore,
)
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import (
    SqliteDocumentShadowIndex,
)
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from tests.conftest import make_storage

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


# -- 装配替身 ---------------------------------------------------------------- #


class _DocIngestor(Ingestor):
    """最小 Ingestor：RawPayload → MemoryUnit（单段文本，盖章时间）。"""

    def operator_type(self) -> IngestOperatorType:
        return IngestOperatorType.INGESTOR

    def health(self) -> None:
        return None

    def ingest(self, payloads: list[RawPayload]) -> list[MemoryUnit]:
        now = datetime.now(UTC)
        units: list[MemoryUnit] = []
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


class _NoopRetriever(Retriever):
    """检索替身——本批用例不触达 recall，仅满足构造期必填。"""

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        return RetrievalResult(items=[])


class _StubEvolver(Evolver):
    """evolve 替身——_RecordingScheduler 不实际跑 Job，evolve 不应被调到。"""

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        raise AssertionError("StubEvolver.evolve 不应被调到（scheduler 仅记录不执行）")


class _AssertingLifecycle(LifecycleManager):
    """文档模式下 supersede/transition 应被 engine 侧绕过——本类调到即断言失败。

    证伪用：若修复缺失（仍走 KVLifecycleManager 范式直调），update/delete 会命中
    这些方法。``sweep`` 返空（本批不测 sweep_expired）。
    """

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope, unit_ids, target) -> None:
        raise AssertionError(
            "文档模式 delete 应绕过 lifecycle.transition，直接落 shadow"
        )

    def supersede(self, scope, unit_id, invalid_at):
        raise AssertionError(
            "文档模式 update SUPERSEDE 应绕过 lifecycle.supersede，直接落 shadow"
        )

    def sweep(self) -> list[SweepTransition]:
        return []


class _RecordingScheduler(InProcessScheduler):
    """记录 submit 入参的 Scheduler 替身（不实际执行 Job）。

    继承 InProcessScheduler 保留 status/cancel，但 submit 不调 job.run——
    让测试断言 Job 字段后无副作用（与 test_cloud_engine.py 同模式）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Job, Channel]] = []

    async def submit(self, job: Job, channel: Channel) -> str:
        self.calls.append((job, channel))
        return "job-1"


def _evolve_job_factory(evolver: Evolver, kv) -> JobFactory:
    """仅注册 EVOLVE 的最小 JobFactory——Spec 装配期固化 kv，evolver 运行时注入。"""
    factory = JobFactory()
    factory.register(JobType.EVOLVE, EvolveJobSpec(kv=kv, evolver=evolver).with_scope)
    return factory


def _doc_engine(tmp_path) -> tuple[AgentEngine, dict]:
    """装配文档模式 AgentEngine：真源 md + 影子索引，KV 不持有 MemoryUnit。"""
    kv = InMemoryKVStore()
    shadow = SqliteDocumentShadowIndex(
        db_path=str(tmp_path / "shadow.db"),
        tokenizer=WhitespaceTokenizer(),
    )
    markdown = LocalMarkdownStore(root=str(tmp_path / "md"))
    manager = CompositeStoreManager(kv=kv, markdown=markdown, shadow_index=shadow)
    domain_store = CompositeDomainStore(
        manager=manager,
        preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK,
        write_document=True,
    )
    manager.bind_domain_store(domain_store)
    index_builder = DocumentIndexBuilder(domain_store)
    evolver = _StubEvolver()
    scheduler = _RecordingScheduler()
    engine = AgentEngine(
        _DocIngestor(),
        index_builder,
        _NoopRetriever(),
        domain_store,
        scheduler,
        evolver,
        _AssertingLifecycle(),
        kv=kv,
        job_factory=_evolve_job_factory(evolver, kv),
    )
    return engine, {
        "kv": kv,
        "shadow": shadow,
        "markdown": markdown,
        "domain_store": domain_store,
        "scheduler": scheduler,
        "evolver": evolver,
    }


def _kv_engine() -> tuple[AgentEngine, dict]:
    """装配非文档模式 AgentEngine：KV 即真源，list/get 经 DomainStore 非文档分支走 KV。"""
    kv = InMemoryKVStore()
    manager = make_storage(kv=kv)
    domain_store = manager.domain_store()  # write_document=False 默认
    index_builder = ForwardIndexBuilder(manager)
    engine = AgentEngine(
        _DocIngestor(),
        index_builder,
        _NoopRetriever(),
        domain_store,
        InProcessScheduler(),
        _StubEvolver(),
        _AssertingLifecycle(),
        kv=kv,
    )
    return engine, {"kv": kv, "domain_store": domain_store}


# -- 文档模式：读路径经 DomainStore（shadow） --------------------------------- #


def test_doc_write_then_list_get_hit(tmp_path) -> None:
    """文档模式 write 后 list 非空、get 命中——读经 DomainStore 分流到 shadow。

    修复前 list 走 ``self._kv``（空）返空、get 抛 NotFoundError。
    """
    engine, parts = _doc_engine(tmp_path)
    assert parts["domain_store"].should_write_document() is True
    assert parts["kv"].scopes() == []  # KV 不持有 MemoryUnit（证伪：读 KV 必空）

    units = asyncio.run(engine.write("hello world", SCOPE))
    assert len(units) == 1
    unit_id = units[0].id

    result = asyncio.run(engine.list(SCOPE))
    assert result.count == 1, f"list 应命中 1 条，实际 {result.count}（读路径未走 shadow）"
    assert [u.id for u in result.items] == [unit_id]

    got = asyncio.run(engine.get(unit_id, SCOPE))
    assert got.id == unit_id
    assert got.segments[0].content == "hello world"


def test_doc_list_units_and_candidate_scopes_nonempty(tmp_path) -> None:
    """``_list_units`` / ``candidate_scopes`` 经 DomainStore 非空（delete/evolve 候选源）。"""
    engine, _ = _doc_engine(tmp_path)
    asyncio.run(engine.write("candidate content", SCOPE))

    units = engine._list_units(SCOPE)  # pylint: disable=protected-access
    assert len(units) == 1, "_list_units 经 domain_store.list 应非空"

    scopes = engine.candidate_scopes()
    assert SCOPE in scopes, f"candidate_scopes 应含 {SCOPE}，实际 {scopes}"


def test_doc_get_not_found_raises(tmp_path) -> None:
    """文档模式 get 不存在的 id 仍抛 NotFoundError（DomainStore.get 缺失省略→engine 抛）。"""
    from jiuwen_memory.common.errors import NotFoundError

    engine, _ = _doc_engine(tmp_path)
    with pytest.raises(NotFoundError):
        asyncio.run(engine.get("no-such-unit", SCOPE))


# -- 文档模式：update SUPERSEDE 绕过 lifecycle + coords 反推 ------------------ #


def test_doc_update_supersede_no_notfound_and_same_md_file(tmp_path) -> None:
    """文档模式 update SUPERSEDE 不抛 NotFoundError + 新版落同 md 文件。

    修复前 ``lifecycle.supersede`` 从 KV 取 unit 必 NotFoundError。修复后 engine 侧
    直接改 old 落 shadow。另验证 ``restore_coords_from_md_filename``：old 经 shadow
    读回 coords（TRANSIENT 键，dumps 剥除）丢失 → 新版若无 coords 反推会落错 md 文件
    （project 维丢失）。这里用 coords dict 传 project（非标量），迫使反推路径生效。
    """
    engine, _ = _doc_engine(tmp_path)
    # project 经 coords（dict，TRANSIENT）传入——read-back 会被 dumps 剥除，逼出反推路径。
    units = asyncio.run(
        engine.write(
            "v1 content",
            SCOPE,
            system_metadata={COORDS_KEY: {"project": "projA"}},
        )
    )
    old = units[0]
    old_md_filename = old.system_metadata[MD_FILENAME_KEY]
    assert old_md_filename.startswith("memory/projA/"), (
        f"旧版应落 memory/projA/ 下，实际 {old_md_filename!r}"
    )

    # SUPERSEDE：修复前此处抛 NotFoundError（lifecycle.supersede 读空 KV）。
    new = asyncio.run(
        engine.update(old.id, SCOPE, MemoryPatch(content="v2 content"))
    )
    assert new.id != old.id
    assert new.supersedes == old.id
    assert new.lifecycle == LifecycleState.ACTIVE
    assert new.system_metadata[MD_FILENAME_KEY] == old_md_filename, (
        "新版应落同 md 文件（coords 经 md_filename 反推 project）；"
        f"新版 {new.system_metadata[MD_FILENAME_KEY]!r} ≠ 旧版 {old_md_filename!r}"
    )

    # 旧版 lifecycle 已落 shadow（绕过 lifecycle.supersede）。
    reloaded_old = asyncio.run(engine.get(old.id, SCOPE))
    assert reloaded_old.lifecycle == LifecycleState.SUPERSEDED
    assert reloaded_old.temporal.t_invalid is not None


def test_doc_update_overwrite_keeps_id(tmp_path) -> None:
    """文档模式 update OVERWRITE 同 id 落 shadow（不走版本链）。"""
    engine, _ = _doc_engine(tmp_path)
    units = asyncio.run(engine.write("orig", SCOPE))
    unit_id = units[0].id

    updated = asyncio.run(
        engine.update(unit_id, SCOPE, MemoryPatch(content="patched", mode=UpdateMode.OVERWRITE))
    )
    assert updated.id == unit_id
    got = asyncio.run(engine.get(unit_id, SCOPE))
    assert got.segments[0].content == "patched"


# -- 文档模式：delete FORGET/ARCHIVE 绕过 lifecycle --------------------------- #


def test_doc_delete_forget_changes_lifecycle(tmp_path) -> None:
    """文档模式 delete FORGET 直接改 lifecycle 落 shadow（绕过 lifecycle.transition）。

    修复前 ``lifecycle.transition`` 读空 KV→lifecycle 不变，但 ``remove(SOFT)`` 退
    检索→真源与索引不一致。修复后直接 ``unit.lifecycle = FORGOTTEN`` 落 shadow。
    """
    engine, _ = _doc_engine(tmp_path)
    units = asyncio.run(engine.write("to forget", SCOPE))
    unit_id = units[0].id

    affected = asyncio.run(
        engine.delete(DeleteSelector(scope=SCOPE, mode=DeleteMode.FORGET, unit_ids=[unit_id]))
    )
    assert affected == [unit_id]

    got = asyncio.run(engine.get(unit_id, SCOPE))
    assert got.lifecycle == LifecycleState.FORGOTTEN


def test_doc_delete_archive_changes_lifecycle(tmp_path) -> None:
    """文档模式 delete ARCHIVE 同 FORGET 范式（target=ARCHIVED）。"""
    engine, _ = _doc_engine(tmp_path)
    units = asyncio.run(engine.write("to archive", SCOPE))
    unit_id = units[0].id

    affected = asyncio.run(
        engine.delete(DeleteSelector(scope=SCOPE, mode=DeleteMode.ARCHIVE, unit_ids=[unit_id]))
    )
    assert affected == [unit_id]
    got = asyncio.run(engine.get(unit_id, SCOPE))
    assert got.lifecycle == LifecycleState.ARCHIVED


def test_doc_delete_by_tags_matches(tmp_path) -> None:
    """文档模式 delete 按 tags 命中——候选扫描经 ``_list_units``（DomainStore）非空。"""
    engine, _ = _doc_engine(tmp_path)
    asyncio.run(engine.write("tagged unit", SCOPE, tags=["red", "blue"]))
    asyncio.run(engine.write("other unit", SCOPE, tags=["green"]))

    affected = asyncio.run(
        engine.delete(DeleteSelector(scope=SCOPE, mode=DeleteMode.FORGET, tags=["red"]))
    )
    assert len(affected) == 1


# -- 文档模式：evolve 传 domain_store --------------------------------------- #


def test_doc_evolve_resolver_reads_shadow(tmp_path) -> None:
    """evolve 把 domain_store 传给 resolver——候选经 DomainStore.list 读 shadow 非空。

    修复前 evolve 传 ``kv=self._kv``（空），PredicateResolver 走 KV 分支读空。
    修复后传 ``kv=self._domain_store``，resolver 走 DomainStore 分支读 shadow。
    """
    engine, parts = _doc_engine(tmp_path)
    asyncio.run(engine.write("evolve candidate", SCOPE))
    scheduler = parts["scheduler"]

    job_id = asyncio.run(engine.evolve(SCOPE, EvolveMode.CONSOLIDATE, Channel.HOT))
    assert job_id == "job-1"
    assert len(scheduler.calls) == 1
    job, channel = scheduler.calls[0]
    assert channel == Channel.HOT
    assert job.scope == SCOPE

    # resolver 由 submit_evolve 经 build_resolver(kv=domain_store) 装配——直接 resolve
    # 验证候选非空（走 DomainStore 分支读 shadow，而非空 KV）。
    outcome = asyncio.run(job._resolver.resolve())  # pylint: disable=protected-access
    assert len(outcome.groups) == 1
    assert len(outcome.groups[0].units) == 1, "evolve 候选应非空（domain_store 读 shadow）"


# -- 非文档模式回归：list/get 仍走 KV --------------------------------------- #


def test_kv_mode_write_then_list_get_hit() -> None:
    """非文档模式（write_document=false）回归：list/get 经 DomainStore 非文档分支走 KV。"""
    engine, parts = _kv_engine()
    assert parts["domain_store"].should_write_document() is False

    units = asyncio.run(engine.write("kv mode content", SCOPE))
    unit_id = units[0].id
    # KV 即真源——写入后 KV 持有 MemoryUnit。
    assert parts["kv"].scopes() != []

    result = asyncio.run(engine.list(SCOPE))
    assert result.count == 1
    assert [u.id for u in result.items] == [unit_id]

    got = asyncio.run(engine.get(unit_id, SCOPE))
    assert got.id == unit_id
    assert got.segments[0].content == "kv mode content"
