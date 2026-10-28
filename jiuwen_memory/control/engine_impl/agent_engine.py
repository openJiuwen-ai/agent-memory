# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentEngine — ToC（个人 agent）方向的引擎：MemoryEngine 独立实现。

引擎分野：CloudEngine 服务 ToB 云侧部署（message_type/profile 路由、
DomainStore 数据面、安全 KV）；本引擎服务 ToC 个人 agent 记忆场景，读取
对齐 CloudEngine——经 DomainStore（list/get/scopes 文档模式分流 shadow）、
metadata 保留原生类型落库、admin_* 不落引擎（API 层直达 PolicyManager）。
不继承任何 Engine：避免与本地最小实现/云侧实现产生隐式耦合，agent 方向的
特化在本文件独立演进。

与 InMemoryEngine 的差异（write 分化面 + scope 语义 + 文档模式 lifecycle 绕过）：

1. **多空间**：不设 ``scope.space == ""`` 本地域校验——delete / 候选枚举扫
   全部 scopes，支撑个人 agent 的 project 分目录等多空间落点。
2. **纯文本模态**：jiuwenswarm 侧记忆 rail（MemoryRail / CodingMemoryRail）
   写入/存储/检索全链路纯文本（工具签名 ``content: str``，存储 .md，检索
   文本向量 + BM25），无 VIDEO 的 uri 载体与 payload_id 幂等需求——
   ``data`` 恒 ``content.encode("utf-8")``、``uri`` 恒空串。非文本模态由
   normalizer 能力门禁在规约前拒绝（UnsupportedCapabilityError）。若未来
   rail 层出现多模态写入工具，需恢复模态特判。
3. **无 procedural / middle 分流**：agent 链路无过程性记忆与中位延迟写入
   语义，write 只保留 infer=true 的派生抽取与缺省直写两路；middle /
   middle_interval 与 InMemoryEngine 一致 pop 不落盘。
4. **文档模式 lifecycle 绕过**：默认装配文档模式（真源 md + shadow，KV 不
   持有 MemoryUnit）。KVLifecycleManager 的 supersede/transition/sweep 都从
   KVStore 取 unit，文档模式下走它必 NotFoundError / 静默失效。update
   SUPERSEDE 与 delete FORGET/ARCHIVE 在 engine 侧绕过 lifecycle 直接改
   unit 落 shadow（对齐 CloudEngine SUPERSEDE 范式，见 doc_mode_support）。

默认装配为文档模式（markdown 真源 + 影子索引 + 看门狗，见
config/document_flag.py 的 resolve_index_builder_default）。
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from jiuwen_memory.common.errors import AgentMemoryError, NotFoundError, ValidationError
from jiuwen_memory.common.log import (
    get_logger,
    metadata_for_log,
    scope_for_log,
)
from jiuwen_memory.common.type_def import (
    CandidateSource,
    Context,
    FilterExpr,
    LifecycleState,
    MemoryUnit,
    MetadataValueType,
    Modality,
    RawPayload,
    Scope,
    Segment,
)
from jiuwen_memory.config.document_flag import resolve_index_builder_default
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.classifier import Classifier, ClassifierProducer
from jiuwen_memory.construction.evolver import Evolver, EvolverProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.construction.source_update import SourceUpdatePlan
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine import EngineProducer, MemoryEngine
from jiuwen_memory.control.engine_impl.doc_mode_support import (
    project_from_context,
    restore_coords_from_md_filename,
)
from jiuwen_memory.control.engine_impl.evolve_dispatch import submit_evolve
from jiuwen_memory.control.engine_impl.schema_update_support import (
    commit_schema_update,
    is_schema_update_candidate,
    prepare_schema_update,
)
from jiuwen_memory.control.engine_impl.sweep_support import run_sweep
from jiuwen_memory.control.engine_impl.temporal_support import (
    matches_delete_selector,
    valid_at,
)
from jiuwen_memory.control.engine_impl.temporal_support import (
    valid_sort_key as _valid_sort_key,
)
from jiuwen_memory.control.jobs import JobFactory, JobFactoryProducer
from jiuwen_memory.control.lifecycle import LifecycleManager, LifecycleProducer
from jiuwen_memory.control.pipeline import MemoryPipeline, PipelineBinding, PipelineProducer
from jiuwen_memory.control.scheduler import Scheduler, SchedulerProducer
from jiuwen_memory.control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteMode,
    DeleteSelector,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
    SweepResult,
    UpdateMode,
)
from jiuwen_memory.ingest.ingestor import Ingestor, IngestorProducer
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name
from jiuwen_memory.storage.types import IndexRemoveMode

logger = get_logger(__name__)

_LIFECYCLE_OF_DELETE = {
    DeleteMode.FORGET: LifecycleState.FORGOTTEN,
    DeleteMode.ARCHIVE: LifecycleState.ARCHIVED,
    DeleteMode.DOWNWEIGHT: LifecycleState.ACTIVE,
}


def _apply_patch(old: MemoryUnit, patch: MemoryPatch) -> MemoryUnit:
    """把非 None 的 patch 字段叠加到旧单元的深拷贝上。"""
    new = copy.deepcopy(old)
    if patch.content is not None:
        # 文本修正：归一到单段，保留原资产（扁平合并）与主模态。
        new.segments = [Segment(content=patch.content, assets=list(old.assets), source=old.source)]
    if patch.tier is not None:
        new.tier = patch.tier
    if patch.tags is not None:
        new.tags = list(patch.tags)
    if patch.system_metadata is not None:
        new.system_metadata.update(patch.system_metadata)
    if patch.user_metadata is not None:
        new.user_metadata.update(patch.user_metadata)
    if patch.t_valid is not None:
        new.temporal.t_valid = patch.t_valid
    if patch.t_invalid is not None:
        new.temporal.t_invalid = patch.t_invalid
    return new


def _now() -> datetime:
    return datetime.now(UTC)


def _downweight_importance(unit: MemoryUnit) -> None:
    raw = unit.system_metadata.get("importance")
    try:
        value = float(raw) if raw is not None else 1.0
    except (ValueError, TypeError):
        value = 1.0
    unit.system_metadata["importance"] = f"{max(0.0, value * 0.5):g}"


@dataclass(frozen=True)
class _ScopedUnitId:
    org: str
    space: str
    user: str
    agent: str
    session: str
    unit_id: str


def _scoped_unit_id(scope: Scope, unit_id: str) -> _ScopedUnitId:
    return _ScopedUnitId(
        org=scope.org,
        space=scope.space,
        user=scope.user,
        agent=scope.agent,
        session=scope.session,
        unit_id=unit_id,
    )


def _permission_context_from_unit(unit: MemoryUnit) -> PermissionContext:
    return PermissionContext(
        resource_type="memory_unit",
        memory_type=str(unit.system_metadata.get("memory_type", "")).strip(),
        pipeline=str(unit.system_metadata.get("pipeline", "")).strip(),
        unit_id=unit.id,
        scope=unit.scope,
        tags=tuple(unit.tags),
        metadata={key: str(value) for key, value in unit.system_metadata.items()},
    )


def _expand_provenance_descendants(
    units: list[tuple[Scope, str, MemoryUnit]],
    seed_ids: set[_ScopedUnitId],
) -> set[_ScopedUnitId]:
    purge_ids = set(seed_ids)
    changed = True
    while changed:
        changed = False
        for scope, _, unit in units:
            unit_ref = _scoped_unit_id(scope, unit.id)
            if unit_ref in purge_ids:
                continue
            if any(
                _scoped_unit_id(scope, parent_id) in purge_ids
                for parent_id in unit.provenance
            ):
                purge_ids.add(unit_ref)
                changed = True
    return purge_ids


class AgentEngine(MemoryEngine):
    """ToC（个人 agent）编排中枢：驱动接入/构建/检索算子与真源 KVStore 完成。"""

    def __init__(
        self,
        ingestor: Ingestor,
        index_builder: IndexBuilder,
        retriever: Retriever,
        domain_store: DomainStore,
        scheduler: Scheduler,
        evolver: Evolver,
        lifecycle: LifecycleManager,
        *,
        kv: KVStore,
        classifier: Classifier | None = None,
        pipeline: MemoryPipeline | None = None,
        job_factory: JobFactory | None = None,
    ) -> None:
        self._ingestor = ingestor
        self._index = index_builder
        self._retriever = retriever
        self._domain_store = domain_store
        self._kv = kv
        self._scheduler = scheduler
        self._evolver = evolver
        self._lifecycle = lifecycle
        self._pipeline = pipeline
        # classifier 可选：infer=false（默认路径）时给原文打 tier+tags；
        # None 时跳过（原文 tier 保持 EPISODIC 默认，向后兼容）。
        self._classifier = classifier
        self._job_factory = job_factory

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.ENGINE

    @property
    def kv(self) -> KVStore:
        """真源 KV 端口（API 层 dreaming 编排读注册表用，见 MemoryEngine.kv）。

        文档模式下 KV 不持有 MemoryUnit（真源在 shadow），本端口仅供 dreaming 注册表
        读写（注册表 key 非 MemoryUnit）；MemoryUnit 读取经 ``_domain_store``。
        """
        return self._kv

    def candidate_scopes(self) -> list[Scope]:
        """返回演进候选数据面中已有记忆的作用域（对齐 CloudEngine，经 DomainStore）。

        文档模式下 KV 不持有 MemoryUnit，默认实现走 ``self.kv.scopes()`` 会漏 scope。
        """
        return self._domain_store.scopes()

    def health(self) -> None:
        return None

    async def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        # 调用级开关（经 metadata 下推，对齐常见记忆层 add(infer=True)）：
        # - infer=true：同步抽取——原文落 /messages/（不建索引），evolver 收集最近10条原文
        #   （指代消解/语境）+ 召回10条相关记忆（去重提示），调 extractor 抽派生落 /memory/。
        # - 缺省：原始落 /memory/ + 建索引，不自动提交演进（由调用方显式 evolve() 触发）。
        # agent 场景无 procedural（过程记忆汇总）与 middle（中期缓冲）路径。
        # 开关按字符串判定（兼容 "true" 与 Python True），业务值保持原生类型落库——
        # 整体 str 化会让数值/布尔在索引里变成 keyword，range 退化为字典序。
        # middle / middle_interval 是调用级开关，不是 unit 持久属性——下面从 meta 剥除。
        meta = dict(system_metadata or {})

        def _is_true(key: str) -> bool:
            return str(meta.get(key, "")).strip().lower() == "true"

        infer = _is_true("infer")

        # agent 场景仅文本模态：jiuwenswarm 侧记忆 rail（MemoryRail /
        # CodingMemoryRail）写入/存储/检索全链路纯文本（工具签名
        # content: str，存储 .md，检索文本向量+BM25），无 VIDEO 的 uri 载体
        # 与 payload_id 幂等需求；非文本模态由 normalizer 能力门禁在规约前
        # 拒绝（UnsupportedCapabilityError）。
        payload = RawPayload(
            id=str(uuid.uuid4()),
            scope=scope,
            modality=source,
            data=content.encode("utf-8"),
            uri="",
            assets=list(assets or []),
            system_metadata=meta,
            user_metadata=dict(user_metadata or {}),
            occurred_at=occurred_at,
        )
        # 两条路径共用：Ingestor 规约。
        units = self._ingestor.ingest([payload])
        for unit in units:
            unit.tags = list(tags or [])
            if "session_id" in meta:
                unit.system_metadata["session_id"] = meta["session_id"]

        binding = self._write_binding(units)
        evolver = binding.evolver if binding is not None else self._evolver
        index_builder = binding.index_builder if binding is not None else self._index
        classifier = binding.classifier if binding is not None else self._classifier

        if infer:
            if evolver is None:
                raise RuntimeError(
                    "AgentEngine.write infer=True requires an Evolver (装配未注入 evolver)"
                )
            result = await asyncio.to_thread(
                evolver.evolve, units, EvolveMode.EXTRACT
            )
            # 落盘产物优先取回传对象：归属判定改写派生单元的 scope 之后，按入参 scope
            # 回读真源会落空。回传为空时回落按 id 回读，兼容不回填该字段的 Evolver 实现。
            derived = list(result.created_units) or [
                self._load(scope, uid) for uid in result.created_ids
            ]
            infer_metadata = {"infer": meta.get("infer")}
            logger.info(
                "AgentEngine.write system_metadata=%s: %d originals, %d derived added, scope=%s",
                metadata_for_log(infer_metadata),
                len(units),
                len(derived),
                scope_for_log(scope),
            )
            return derived

        # 默认路径（infer=false）：classifier 给原文打 tier+tags → 落 /memory/{id} + 建索引。
        # classifier 为 None 时跳过（tier 保持 EPISODIC 默认，向后兼容）。
        if classifier is not None:
            classifier.classify(units)
        # 记忆写入只经 IndexBuilder：交付 Storage + 建 hot 轻量索引由其统一编排。
        await asyncio.to_thread(index_builder.build, units)
        return units

    async def batch_write(
        self,
        items: list[BatchWriteItem],
        *,
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        outcomes: list[BatchWriteOutcome] = []
        for index, item in enumerate(items):
            try:
                units = await self.write(
                    item.content,
                    item.scope,
                    item.source,
                    assets=item.assets,
                    tags=item.tags,
                    system_metadata=item.system_metadata,
                    user_metadata=item.user_metadata,
                    occurred_at=item.occurred_at,
                )
                outcomes.append(BatchWriteOutcome(index=index, item=item, units=units))
            except Exception as exc:
                is_domain_error = isinstance(exc, AgentMemoryError)
                if not is_domain_error:
                    logger.exception("unexpected batch write failure at item %s", index)
                outcomes.append(
                    BatchWriteOutcome(
                        index=index,
                        item=item,
                        error=str(exc) if is_domain_error else "unexpected batch write failure",
                        error_type=type(exc).__name__ if is_domain_error else "InternalError",
                    )
                )
                if not continue_on_error:
                    outcomes.extend(
                        BatchWriteOutcome(
                            index=skipped_index,
                            item=skipped_item,
                            error="skipped after previous item failed",
                            error_type="Skipped",
                        )
                        for skipped_index, skipped_item in enumerate(items[index + 1:], index + 1)
                    )
                    break
        return BatchWriteResult(outcomes=outcomes)

    async def recall(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        binding = self._recall_binding(query)
        retriever = binding.retriever if binding is not None else self._retriever
        return retriever.retrieve(scope, query)

    async def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | None = None,
    ) -> MemoryListResult:
        if offset < 0:
            raise ValidationError("offset must be >= 0")
        if limit <= 0:
            raise ValidationError("limit must be > 0")
        result = self._domain_store.list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            extensions=extensions,
            filters=filters,
        )
        return MemoryListResult(items=result.items, count=result.count)

    async def permission_context_for_unit(
        self, unit_id: str, scope: Scope
    ) -> PermissionContext:
        return _permission_context_from_unit(self._load(scope, unit_id))

    async def list_with_permission_contexts(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | None = None,
    ) -> tuple[MemoryListResult, list[PermissionContext]]:
        result = await self.list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            extensions=extensions,
            filters=filters,
        )
        contexts = [_permission_context_from_unit(unit) for unit in result.items]
        return result, contexts

    async def permission_contexts_for_delete(
        self, selector: DeleteSelector
    ) -> list[PermissionContext]:
        # 多空间：selector 未指定 scope 时扫全部 scopes（不滤 space）。
        scopes = (
            [selector.scope]
            if selector.scope is not None
            else list(self._domain_store.scopes())
        )
        if not scopes:
            scopes = [Scope()]
        contexts: list[PermissionContext] = []
        for scope in scopes:
            for unit in self._list_units(scope):
                if matches_delete_selector(unit, selector):
                    contexts.append(_permission_context_from_unit(unit))
        return contexts

    async def get(
        self, unit_id: str, scope: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        if as_of is None:
            return self._load(scope, unit_id)

        candidates = []
        for unit in self._version_family(scope, unit_id):
            if unit.lifecycle == LifecycleState.FORGOTTEN:
                continue
            if valid_at(unit, as_of):
                candidates.append(unit)
        if not candidates:
            logger.warning(
                "AgentEngine.get as_of miss: unit_id=%s scope=%s as_of=%s",
                unit_id,
                scope_for_log(scope),
                as_of,
            )
            raise NotFoundError("memory_unit", unit_id)
        selected = max(candidates, key=_valid_sort_key)
        logger.debug(
            "AgentEngine.get as_of hit: unit_id=%s selected_id=%s scope=%s as_of=%s",
            unit_id,
            selected.id,
            scope_for_log(scope),
            as_of,
        )
        return selected

    def requires_update_preparation(self, unit: MemoryUnit, patch: MemoryPatch) -> bool:
        return is_schema_update_candidate(unit, patch)

    async def prepare_update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch
    ) -> SourceUpdatePlan | None:
        old = self._load(scope, unit_id)
        return await self._prepare_update_from_old(old, patch)

    async def _prepare_update_from_old(
        self, old: MemoryUnit, patch: MemoryPatch
    ) -> SourceUpdatePlan | None:
        """Prepare against the same source snapshot used for the eligibility check."""
        if not self.requires_update_preparation(old, patch):
            return None
        return await prepare_schema_update(
            self._evolver_for_unit, old, _apply_patch(old, patch), patch
        )

    async def commit_update(self, plan: SourceUpdatePlan) -> MemoryUnit:
        return await commit_schema_update(
            self._evolver_for_unit, plan, index_for=self._index_for_unit
        )

    async def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch
    ) -> MemoryUnit:
        old = self._load(scope, unit_id)
        if self.requires_update_preparation(old, patch):
            plan = await self._prepare_update_from_old(old, patch)
            if plan is not None:
                return await self.commit_update(plan)
        new = _apply_patch(old, patch)
        if patch.mode == UpdateMode.OVERWRITE:
            new.id = old.id
            self._index.update([new])
            logger.info(
                "AgentEngine.update overwrite: unit_id=%s scope=%s",
                new.id,
                scope_for_log(scope),
            )
        else:  # SUPERSEDE：新 id、记版本链，旧版标记 superseded
            new.id = str(uuid.uuid4())
            new.supersedes = old.id
            new.lifecycle = LifecycleState.ACTIVE
            if patch.t_valid is None:
                new.temporal.t_valid = _now()
            if self._domain_store.should_write_document():
                # 文档模式 supersede：old 经 shadow 读回 coords 是 TRANSIENT 键（dumps
                # 剥除），deepcopy 的 new 无 coords → domain_store.add 内 _project_of
                # 读 coords 落空 → 新版落错 md 文件。从 old 的 md_filename（落盘键，不
                # 剥除）反推 project 回写 new 的 coords（对齐 CloudEngine，F08 §5.2.1）。
                restore_coords_from_md_filename(new, old)
            # 新版先落地再废旧版：任何时刻都有一个可读版本。反过来的话，若 build 失败，
            # 旧版已 SUPERSEDED 而新版尚未存在，这条记忆既退出活跃召回又无新版可读。
            self._index.build([new])
            if self._domain_store.should_write_document():
                # 文档模式绕过 KV lifecycle（doc-mode-supersede-kv-lifecycle-break）：
                # KVLifecycleManager.supersede 从 KVStore 取 unit，文档模式真源在
                # shadow、KV 不持有 MemoryUnit，走它必 NotFoundError。直接改 old 落
                # shadow（content_hash 不变→shadow.update_units 只覆写 unit_json +
                # lifecycle 投影列，F08 决策三）。对齐 CloudEngine。
                old.lifecycle = LifecycleState.SUPERSEDED
                old.temporal.t_invalid = new.temporal.t_valid or _now()
                self._index.update([old])
            else:
                old = self._lifecycle.supersede(scope, old.id, new.temporal.t_valid)
                self._index.update([old])
            logger.info(
                "AgentEngine.update supersede: old_id=%s new_id=%s scope=%s t_valid=%s",
                old.id,
                new.id,
                scope_for_log(scope),
                new.temporal.t_valid,
            )
        return new

    async def delete(self, selector: DeleteSelector) -> list[str]:
        selector_is_empty = (
            not selector.unit_ids
            and not selector.tags
            and selector.before is None
            and selector.filters is None
        )
        if selector_is_empty:
            logger.warning("AgentEngine.delete rejected empty selector")
            raise ValidationError("DeleteSelector requires unit_ids, tags, before, or filters")

        # 多空间：selector 未指定 scope 时扫全部 scopes（不滤 space）。
        scopes = (
            [selector.scope]
            if selector.scope is not None
            else list(self._domain_store.scopes())
        )
        if not scopes:
            scopes = [Scope()]
        scanned: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in scopes:
            scanned.extend((scope, unit.id, unit) for unit in self._list_units(scope))

        matches = [
            (scope, unit_id, unit)
            for scope, unit_id, unit in scanned
            if matches_delete_selector(unit, selector)
        ]
        affected = [unit.id for _, _, unit in matches]
        if not affected:
            logger.info(
                "AgentEngine.delete no matches: mode=%s scope=%s",
                selector.mode.value,
                scope_for_log(selector.scope),
            )
            return []

        if selector.mode == DeleteMode.PURGE:
            purge_ids = _expand_provenance_descendants(
                scanned,
                {_scoped_unit_id(scope, unit.id) for scope, _, unit in matches},
            )
            purged_units: list[MemoryUnit] = []
            for scope, _, unit in scanned:
                if _scoped_unit_id(scope, unit.id) in purge_ids:
                    purged_units.append(unit)
            # 物理删除：记忆本体与派生索引由 IndexBuilder 一并移除。
            self._index.remove(purged_units)
            logger.info(
                "AgentEngine.delete purge: count=%d scope=%s",
                len(purged_units),
                scope_for_log(selector.scope),
            )
            return [unit.id for unit in purged_units]

        if selector.mode == DeleteMode.DOWNWEIGHT:
            update_index: list[MemoryUnit] = []
            for scope, _, unit in matches:
                _downweight_importance(unit)
                update_index.append(unit)
            self._index.update(update_index)
            logger.info(
                "AgentEngine.delete downweight: count=%d scope=%s",
                len(affected),
                scope_for_log(selector.scope),
            )
            return affected

        target = _LIFECYCLE_OF_DELETE[selector.mode]
        if self._domain_store.should_write_document():
            # 文档模式绕过 KV lifecycle（doc-mode-supersede-kv-lifecycle-break）：
            # KVLifecycleManager.transition 从 KVStore 取 unit，文档模式真源在 shadow、
            # KV 不持有 MemoryUnit，走它读空→lifecycle 不变但 remove(SOFT) 退索引→真源
            # 与索引不一致。直接改 unit.lifecycle 落 shadow（对齐 update SUPERSEDE 绕过
            # 范式；DOWNWEIGHT 在上方已单独处理，此处只 FORGET/ARCHIVE）。
            for _, _, unit in matches:
                unit.lifecycle = target
            self._index.update([unit for _, _, unit in matches])
        else:
            by_scope: dict[tuple[str, str, str, str, str], tuple[Scope, list[str]]] = {}
            for matched_scope, _, unit in matches:
                key = (
                    matched_scope.org,
                    matched_scope.space,
                    matched_scope.user,
                    matched_scope.agent,
                    matched_scope.session,
                )
                _, unit_ids = by_scope.setdefault(key, (matched_scope, []))
                unit_ids.append(unit.id)
            for matched_scope, unit_ids in by_scope.values():
                self._lifecycle.transition(matched_scope, unit_ids, target)
        # 非破坏式：lifecycle 已改（文档模式落 shadow / 非文档回写 KV）并保留，
        # 此处仅让检索索引退出检索。
        self._index.remove([unit for _, _, unit in matches], mode=IndexRemoveMode.SOFT)
        logger.info(
            "AgentEngine.delete transition: mode=%s target=%s count=%d scope=%s",
            selector.mode.value,
            _LIFECYCLE_OF_DELETE[selector.mode].value,
            len(affected),
            scope_for_log(selector.scope),
        )
        return affected

    async def sweep_expired(self) -> SweepResult:
        # C-03：lifecycle 只纯计算 transition，索引清理与真源回写由本编排完成。
        transitions = self._lifecycle.sweep()
        return run_sweep(
            transitions,
            self._lifecycle,
            lambda units: self._index.remove(units, mode=IndexRemoveMode.SOFT),
        )

    async def purge_space(self, org: str, space: str) -> list[str]:
        purged_units: list[MemoryUnit] = []
        for scope in [
            candidate
            for candidate in self._domain_store.scopes()
            if candidate.org == org and candidate.space == space
        ]:
            units = self._list_units(scope)
            purged_units.extend(units)
        self._index.remove(purged_units)
        return [unit.id for unit in purged_units]

    async def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        candidate: CandidateSource | dict | None = None,
        buckets: list[Scope] | None = None,
        denied_scopes: list[str] | None = None,
    ) -> str | None:
        if self._job_factory is None:
            raise RuntimeError(
                "evolve requires job_factory, please configure "
                "engine.default.job_factory"
            )
        if self._evolver is None:
            raise RuntimeError(
                "AgentEngine.evolve requires an Evolver (装配未注入 evolver)"
            )
        # E-06：evolver 必传注入——Job 使用 Engine 装配的同一实例，
        # 不允许 Spec 侧自行解析另一套。纯执行（鉴权在 API 层）：
        # candidate→resolver 翻译 + 一次性 EvolveJob 提交。
        return await submit_evolve(
            scope=scope,
            mode=mode,
            channel=channel,
            candidate=candidate,
            # 传 DomainStore 非 KV：candidate_resolver 双适配（KVStore | DomainStore），
            # 文档模式走 DomainStore 分支读 shadow 候选（对齐 CloudEngine）。
            kv=self._domain_store,
            scheduler=self._scheduler,
            job_factory=self._job_factory,
            evolver=self._evolver,
            retriever=self._retriever,
            buckets=buckets,
            denied_scopes=denied_scopes,
        )

    async def admin_get(self, key: str) -> str:  # 由 API 层直达 PolicyManager
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_set(self, key: str, value: str) -> None:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_all(self) -> dict[str, str]:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def read_md_file(
        self,
        context: Context,
        md_filename: str,
        *,
        line_range: tuple[int, int] | None = None,
    ) -> list[MemoryUnit]:
        """异步协程：委托 DomainStore.read_md_file（``asyncio.to_thread``），F08。

        与 :meth:`CloudEngine.read_md_file` 同构——project 从 ``context.extensions``
        拆出（:func:`project_from_context`），scope 从 ``context.scope`` 取，经
        ``asyncio.to_thread`` 委托 DomainStore（Engine 协程一律 to_thread 桥接同步
        DomainStore，行为铁律 2）。
        """
        return await asyncio.to_thread(
            self._domain_store.read_md_file,
            context.scope,
            md_filename,
            project=project_from_context(context),
            line_range=line_range,
        )

    async def edit_md_file(
        self,
        context: Context,
        md_filename: str,
        old_text: str,
        new_text: str,
    ) -> list[MemoryUnit]:
        """异步协程：委托 DomainStore.edit_md_file（``asyncio.to_thread``），F08。

        与 :meth:`CloudEngine.edit_md_file` 同构。AgentEngine 默认文档模式，DomainStore
        内部完成继承式 SUPERSEDE 同步（scope 取 ``context.scope``——AgentEngine 多空间、
        scope 由 API 层鉴权后传入，Engine 信任）。
        """
        return await asyncio.to_thread(
            self._domain_store.edit_md_file,
            context.scope,
            md_filename,
            old_text,
            new_text,
            project=project_from_context(context),
        )

    def _write_binding(self, units: list[MemoryUnit]) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_write(units)

    def _evolver_for_unit(self, unit: MemoryUnit) -> Evolver | None:
        binding = self._write_binding([unit])
        return binding.evolver if binding is not None else self._evolver

    def _index_for_unit(self, unit: MemoryUnit) -> IndexBuilder:
        binding = self._write_binding([unit])
        return binding.index_builder if binding is not None else self._index

    def _recall_binding(self, query: RetrievalQuery) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_recall(query)

    def _load(self, scope: Scope, unit_id: str) -> MemoryUnit:
        """从真源点读（对齐 CloudEngine，经 DomainStore 文档模式分流到 shadow）。"""
        units = self._domain_store.get(scope, [unit_id])
        if not units:
            raise NotFoundError("memory_unit", unit_id)
        return units[0]

    def _list_units(self, scope: Scope) -> list[MemoryUnit]:
        # 经 DomainStore 列建索引记忆：文档模式走 shadow.list_units，非文档走 KV.list。
        # 版本链（SUPERSEDE/supersedes）只在建索引记忆间；原文 /messages/ 无版本链。
        return self._domain_store.list(scope, limit=1_000_000).items

    def _version_family(self, scope: Scope, unit_id: str) -> list[MemoryUnit]:
        units_by_id = {unit.id: unit for unit in self._list_units(scope)}
        if unit_id not in units_by_id:
            raise NotFoundError("memory_unit", unit_id)

        neighbors: dict[str, set[str]] = {uid: set() for uid in units_by_id}
        for unit in units_by_id.values():
            if unit.supersedes in units_by_id:
                neighbors[unit.id].add(unit.supersedes)
                neighbors[unit.supersedes].add(unit.id)

        seen: set[str] = set()
        pending = [unit_id]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(neighbors[current] - seen)
        return [units_by_id[uid] for uid in seen]


# -- 注册到 EngineProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@EngineProducer.register("agent")
def _build(config):
    # 参考 InMemoryEngine._build 同构装配：index_builder 缺省经公共函数
    # resolve_index_builder_default（文档模式 → document 薄编排层）；classifier /
    # pipeline / job_factory 按命名空间具名实例可选注入。
    ib_default = resolve_index_builder_default(config)

    def _opt_classifier():
        ctx = config.ctx
        ns = ctx.namespaces.get(ClassifierProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        return ClassifierProducer.build_named("default", ctx)

    def _opt_pipeline():
        ctx = config.ctx
        ns = ctx.namespaces.get(PipelineProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        return PipelineProducer.build_named("default", ctx)

    # JobFactory 可选注入——config 声明了 job_factory 命名空间具名实例则注入，
    # None 时 evolve 报错（向后兼容——纯默认配置不走演进）。
    def _opt_job_factory():
        ctx = config.ctx
        ns = ctx.namespaces.get(JobFactoryProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        import jiuwen_memory.control.jobs_impl as _ji  # noqa: F401
        _ = _ji
        return JobFactoryProducer.build_named("default", ctx)

    manager = StoreManagerProducer.resolve(config)
    return AgentEngine(
        IngestorProducer.dep(config, default="simple"),
        IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        RetrieverProducer.dep(config, default="pipeline"),
        manager.domain_store(resolve_name(config, "domain_store")),
        SchedulerProducer.dep(config, default="in_process"),
        EvolverProducer.dep(config, default="orchestrating"),
        LifecycleProducer.dep(config, default="kv"),
        kv=manager.kv(resolve_name(config, "kv_store")),
        classifier=_opt_classifier(),
        pipeline=_opt_pipeline(),
        job_factory=_opt_job_factory(),
    )
