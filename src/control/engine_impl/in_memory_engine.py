"""最小实现：:class:`~control.engine.MemoryEngine`（接口层各语义的编排中枢）。

串起 write→recall→get（及 update/delete/evolve），按架构 §14 Write 路径：
规约（Ingestor）→ 真源落盘（UnitStore）→ hot 索引（IndexBuilder）→ 提交
background 演进（Scheduler）。接入/构建/检索算子与真源 Store 由装配注入；
``admin_*`` 在本最小实现里不落引擎（由 API 层直达 PolicyManager）。
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from common.errors import AgentMemoryError, NotFoundError, ValidationError
from common.log import get_logger
from common.type_def import (
    MEMORY_KEY_PREFIX,
    FilterExpr,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    RawPayload,
    Scope,
    Segment,
    memory_key,
)
from common.type_def.memory_codec import dumps, loads
from construction import EvolveMode
from construction.classifier import Classifier, ClassifierProducer
from construction.evolver import Evolver, EvolverProducer
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from control.base import ControlOperatorType
from control.engine import EngineProducer, MemoryEngine
from control.engine_impl.list_support import list_page
from control.jobs import JobFactory, JobFactoryProducer, JobType
from control.lifecycle import LifecycleManager, LifecycleProducer
from control.pipeline import MemoryPipeline, PipelineBinding, PipelineProducer
from control.scheduler import Scheduler, SchedulerProducer
from control.types import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteMode,
    DeleteSelector,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
    UpdateMode,
)
from ingest.ingestor import Ingestor, IngestorProducer
from retrieval.retriever import Retriever, RetrieverProducer
from retrieval.types import RetrievalQuery, RetrievalResult
from storage.kv import KvProducer, KVStore

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
    if patch.metadata is not None:
        new.metadata.update(patch.metadata)
    if patch.t_valid is not None:
        new.temporal.t_valid = patch.t_valid
    if patch.t_invalid is not None:
        new.temporal.t_invalid = patch.t_invalid
    return new


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    valid_from = unit.temporal.t_valid
    invalid_from = unit.temporal.t_invalid
    has_non_positive_validity_window = (
        valid_from is not None and invalid_from is not None and invalid_from <= valid_from
    )
    if has_non_positive_validity_window:
        return as_of < invalid_from
    if valid_from is not None and as_of < valid_from:
        return False
    if invalid_from is not None and as_of >= invalid_from:
        return False
    return True


def _valid_sort_key(unit: MemoryUnit) -> datetime:
    return unit.temporal.t_valid or datetime.min.replace(tzinfo=timezone.utc)


def _downweight_importance(unit: MemoryUnit) -> None:
    raw = unit.metadata.get("importance")
    try:
        value = float(raw) if raw is not None else 1.0
    except ValueError:
        value = 1.0
    unit.metadata["importance"] = f"{max(0.0, value * 0.5):g}"


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


def _ensure_local_scope(scope: Scope) -> None:
    if scope.space:
        raise ValidationError("InMemoryEngine only supports scope.space == ''")


def _permission_context_from_unit(unit: MemoryUnit) -> PermissionContext:
    return PermissionContext(
        resource_type="memory_unit",
        memory_type=str(unit.metadata.get("memory_type", "")).strip(),
        pipeline=str(unit.metadata.get("pipeline", "")).strip(),
        unit_id=unit.id,
        scope=unit.scope,
        tags=tuple(unit.tags),
        metadata=dict(unit.metadata),
    )


def _matches_delete_selector(unit: MemoryUnit, selector: DeleteSelector) -> bool:
    wanted_ids = set(selector.unit_ids)
    wanted_tags = set(selector.tags)
    if wanted_ids and unit.id not in wanted_ids:
        return False
    if wanted_tags and not wanted_tags.intersection(unit.tags):
        return False
    if selector.before is not None:
        t_event = unit.temporal.t_event
        if t_event is None or t_event >= selector.before:
            return False
    return True


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


class InMemoryEngine(MemoryEngine):
    """编排接口层各语义；驱动接入/构建/检索算子与真源 Store 完成。"""

    def __init__(
        self,
        ingestor: Ingestor,
        index_builder: IndexBuilder,
        retriever: Retriever,
        kv: KVStore,
        scheduler: Scheduler,
        evolver: Evolver,
        lifecycle: LifecycleManager,
        classifier: Classifier | None = None,
        pipeline: MemoryPipeline | None = None,
        job_factory: JobFactory | None = None,
        middle_interval: int = 50,
    ) -> None:
        self._ingestor = ingestor
        self._index = index_builder
        self._retriever = retriever
        self._kv = kv  # 真源：裸 kv 存序列化字节
        self._scheduler = scheduler
        self._evolver = evolver
        self._lifecycle = lifecycle
        self._pipeline = pipeline
        # classifier 可选：infer=false（默认路径）时给原文打 tier+tags；
        # None 时跳过（原文 tier 保持 EPISODIC 默认，向后兼容）。
        self._classifier = classifier
        self._job_factory = job_factory
        self._middle_interval = middle_interval

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.ENGINE

    def health(self) -> None:
        return None

    def _write_binding(self, units: list[MemoryUnit]) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_write(units)

    def _recall_binding(self, query: RetrievalQuery) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_recall(query)

    async def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        _ensure_local_scope(scope)
        # 调用级开关（经 metadata 下推，对齐 mem0 add(infer=True)）：
        # - procedural=true：过程记忆抽取——原文不落 KV，evolver 让 extractor 把本轮汇总成
        #   1 条 PROCEDURAL 执行历史，落 /memory/ 建索引；不走去重、不收集 context。
        # - infer=true：同步抽取——原文落 /messages/（不建索引），evolver 收集最近10条原文
        #   （指代消解/语境）+ 召回10条相关记忆（去重提示），调 extractor 抽派生落 /memory/。
        # - infer=true + middle=true：中期缓冲子路径——原文落 /memory/ + tier=WORKING +
        #   建索引 + 提交 MiddleToLongJob 定时转长期。
        # - 缺省：原始落 /memory/ + 建索引，不自动提交演进（由调用方显式 evolve() 触发）。
        # 开关按字符串判定（兼容 "true" 与 Python True），业务值保持原生类型落库——
        # 整体 str 化会让数值/布尔在索引里变成 keyword，range 退化为字典序。
        meta = dict(metadata or {})

        def _is_true(key: str) -> bool:
            return str(meta.get(key, "")).strip().lower() == "true"

        procedural = _is_true("procedural")
        infer = _is_true("infer")
        middle = _is_true("middle")  # 二级开关（仅在 infer=true 下生效）
        if middle and not infer and not procedural:
            raise ValueError(
                "metadata.middle=true requires infer=true (middle 是 infer 下的二级开关)"
            )

        payload = RawPayload(
            id=str(uuid.uuid4()),
            scope=scope,
            modality=source,
            data=content.encode("utf-8"),
            metadata=meta,
            occurred_at=occurred_at,
        )
        # 三条路径共用：Ingestor 规约。
        units = self._ingestor.ingest([payload])
        for unit in units:  # 接入层不带 assets/tags，由引擎按 write 入参补齐
            if assets and unit.segments:  # write 入参的 assets 归到首段（接入产出的单段投影）
                unit.segments[0].assets = list(assets)
            unit.tags = list(tags or [])
            if middle:  # 中期缓冲标记——MiddleToLongJob 据此过滤候选
                unit.metadata["middle"] = "true"
            if "session_id" in meta:
                unit.metadata["session_id"] = meta["session_id"]

        binding = self._write_binding(units)
        evolver = binding.evolver if binding is not None else self._evolver
        index_builder = binding.index_builder if binding is not None else self._index
        classifier = binding.classifier if binding is not None else self._classifier

        # procedural 优先（与现 procedural > infer 互斥逻辑一致）
        if procedural:
            if evolver is None:
                raise RuntimeError(
                    "Engine.write procedural=True requires an Evolver (装配未注入 evolver)"
                )
            result = await asyncio.to_thread(
                evolver.evolve, units, EvolveMode.EXTRACT
            )
            derived = [self._load(scope, uid) for uid in result.created_ids]
            logger.info(
                "Engine.write procedural=True: %d originals, %d derived added, scope=%s",
                len(units),
                len(derived), scope,
            )
            return derived

        # infer=true 下按 middle 二级分流
        if infer:
            if middle:
                return await self._write_middle_path(units, scope, index_builder, evolver)
            # 既有 infer=true 同步抽取路径，不动
            if evolver is None:
                raise RuntimeError(
                    "Engine.write infer=True requires an Evolver (装配未注入 evolver)"
                )
            result = await asyncio.to_thread(
                evolver.evolve, units, EvolveMode.EXTRACT
            )
            derived = [self._load(scope, uid) for uid in result.created_ids]
            logger.info(
                "Engine.write infer=True: %d originals, %d derived added, scope=%s",
                len(units),
                len(derived), scope,
            )
            return derived

        # 默认路径（infer=false）：classifier 给原文打 tier+tags → 落 /memory/{id} + 建索引。
        # classifier 为 None 时跳过（tier 保持 EPISODIC 默认，向后兼容）。
        if classifier is not None:
            classifier.classify(units)
        await asyncio.to_thread(self._write_default_to_kv, scope, units)
        await asyncio.to_thread(index_builder.build, units)  # hot 轻量索引
        return units

    # ---- 中期缓冲子路径 ----

    async def _write_middle_path(
        self,
        units: list[MemoryUnit],
        scope: Scope,
        index_builder: IndexBuilder,
        evolver: Evolver,
    ) -> list[MemoryUnit]:
        """中期缓冲子路径：原文落 /memory/ + 建索引 + tier=WORKING + 提交定时 MiddleToLongJob。"""
        if self._job_factory is None:
            raise RuntimeError(
                "middle path requires job_factory, please configure "
                "engine.default.job_factory"
            )
        if evolver is None:
            raise RuntimeError(
                "Engine.write middle=true requires an Evolver (装配未注入 evolver)"
            )

        for unit in units:
            unit.tier = MemoryTier.WORKING
            unit.metadata["middle"] = "true"
        await asyncio.to_thread(self._write_middle_to_kv, scope, units)
        await asyncio.to_thread(index_builder.build, units)

        job = self._job_factory.get_job(
            JobType.MIDDLE_TO_LONG,
            scope=scope,
            interval=self._middle_interval,
            evolver=evolver,
            index=index_builder,
        )
        await self._scheduler.submit(job, channel=Channel.BACKGROUND)
        logger.info(
            "Engine.write middle=True: %d originals buffered, scope=%s interval=%s",
            len(units),
            scope,
            self._middle_interval,
        )
        return units

    def _write_middle_to_kv(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """``asyncio.to_thread`` 只接 callable + args，抽成同步方法以便包装。"""
        for unit in units:
            self._kv.insert(scope, memory_key(unit.id), dumps(unit))

    def _write_default_to_kv(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """``asyncio.to_thread`` 只接 callable + args，抽成同步方法以便包装。"""
        for unit in units:
            self._kv.insert(scope, memory_key(unit.id), dumps(unit))

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
                    metadata=item.metadata,
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
        _ensure_local_scope(scope)
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
        _ensure_local_scope(scope)
        return list_page(
            self._kv,
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            extensions=extensions,
            filters=filters,
        )

    async def permission_context_for_unit(
        self, unit_id: str, scope: Scope
    ) -> PermissionContext:
        _ensure_local_scope(scope)
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
        if selector.scope is not None:
            _ensure_local_scope(selector.scope)
        scopes = (
            [selector.scope]
            if selector.scope is not None
            else [scope for scope in self._kv.scopes() if not scope.space]
        )
        if not scopes:
            scopes = [Scope()]
        contexts: list[PermissionContext] = []
        for scope in scopes:
            for unit in self._list_units(scope):
                if _matches_delete_selector(unit, selector):
                    contexts.append(_permission_context_from_unit(unit))
        return contexts

    def _load(self, scope: Scope, unit_id: str) -> MemoryUnit:
        """从真源读字节并反序列化（产出结果的边界点）。"""
        try:
            raw = self._kv.get(scope, memory_key(unit_id))
        except NotFoundError:
            raise NotFoundError("memory_unit", unit_id) from None
        unit = loads(raw)
        if unit is None:
            raise NotFoundError("memory_unit", unit_id)
        return unit

    def _list_units(self, scope: Scope) -> list[MemoryUnit]:
        # 只列建索引记忆（/memory/ 前缀）。loads 对非 MemoryUnit 记录返回 None，自然过滤。
        # 版本链（SUPERSEDE/supersedes）只在建索引记忆间；原文 /messages/ 无版本链。
        units = []
        for _, raw in self._kv.scan(scope, prefix=MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is not None:
                units.append(unit)
        return units

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

    async def get(
        self, unit_id: str, scope: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        _ensure_local_scope(scope)
        if as_of is None:
            return self._load(scope, unit_id)

        candidates = []
        for unit in self._version_family(scope, unit_id):
            if unit.lifecycle == LifecycleState.FORGOTTEN:
                continue
            if _valid_at(unit, as_of):
                candidates.append(unit)
        if not candidates:
            logger.warning(
                "Engine.get as_of miss: unit_id=%s scope=%s as_of=%s",
                unit_id,
                scope,
                as_of,
            )
            raise NotFoundError("memory_unit", unit_id)
        selected = max(candidates, key=_valid_sort_key)
        logger.debug(
            "Engine.get as_of hit: unit_id=%s selected_id=%s scope=%s as_of=%s",
            unit_id,
            selected.id,
            scope,
            as_of,
        )
        return selected

    async def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch
    ) -> MemoryUnit:
        _ensure_local_scope(scope)
        old = self._load(scope, unit_id)
        new = _apply_patch(old, patch)
        if patch.mode == UpdateMode.OVERWRITE:
            new.id = old.id
            self._kv.update(scope, memory_key(new.id), dumps(new))
            self._index.update([new])
            logger.info("Engine.update overwrite: unit_id=%s scope=%s", new.id, scope)
        else:  # SUPERSEDE：新 id、记版本链，旧版标记 superseded
            new.id = str(uuid.uuid4())
            new.supersedes = old.id
            new.lifecycle = LifecycleState.ACTIVE
            if patch.t_valid is None:
                new.temporal.t_valid = _now()
            self._kv.insert(scope, memory_key(new.id), dumps(new))
            old = self._lifecycle.supersede(scope, old.id, new.temporal.t_valid)
            self._index.update([old])
            self._index.build([new])
            logger.info(
                "Engine.update supersede: old_id=%s new_id=%s scope=%s t_valid=%s",
                old.id,
                new.id,
                scope,
                new.temporal.t_valid,
            )
        return new

    async def delete(self, selector: DeleteSelector) -> list[str]:
        selector_is_empty = (
            not selector.unit_ids and not selector.tags and selector.before is None
        )
        if selector_is_empty:
            logger.warning("Engine.delete rejected empty selector")
            raise ValidationError("DeleteSelector requires unit_ids, tags, or before")

        if selector.scope is not None:
            _ensure_local_scope(selector.scope)
        scopes = (
            [selector.scope]
            if selector.scope is not None
            else [scope for scope in self._kv.scopes() if not scope.space]
        )
        if not scopes:
            scopes = [Scope()]
        scanned: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in scopes:
            for unit_id, raw in self._kv.scan(scope):
                unit = loads(raw)
                if unit is not None:
                    scanned.append((scope, unit_id, unit))

        matches = [
            (scope, unit_id, unit)
            for scope, unit_id, unit in scanned
            if _matches_delete_selector(unit, selector)
        ]
        affected = [unit.id for _, _, unit in matches]
        if not affected:
            logger.info(
                "Engine.delete no matches: mode=%s scope=%s",
                selector.mode.value,
                selector.scope,
            )
            return []

        if selector.mode == DeleteMode.PURGE:
            purge_ids = _expand_provenance_descendants(
                scanned,
                {_scoped_unit_id(scope, unit.id) for scope, _, unit in matches},
            )
            purged_units: list[MemoryUnit] = []
            for scope, unit_id, unit in scanned:
                if _scoped_unit_id(scope, unit.id) in purge_ids:
                    self._kv.delete(scope, unit_id)
                    purged_units.append(unit)
            self._index.remove(purged_units)
            logger.info(
                "Engine.delete purge: count=%d scope=%s",
                len(purged_units),
                selector.scope,
            )
            return [unit.id for unit in purged_units]

        if selector.mode == DeleteMode.DOWNWEIGHT:
            update_index: list[MemoryUnit] = []
            for scope, unit_id, unit in matches:
                _downweight_importance(unit)
                self._kv.update(scope, unit_id, dumps(unit))
                update_index.append(unit)
            self._index.update(update_index)
            logger.info(
                "Engine.delete downweight: count=%d scope=%s",
                len(affected),
                selector.scope,
            )
            return affected

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
            self._lifecycle.transition(
                matched_scope,
                unit_ids,
                _LIFECYCLE_OF_DELETE[selector.mode],
            )
        self._index.remove([unit for _, _, unit in matches])
        logger.info(
            "Engine.delete transition: mode=%s target=%s count=%d scope=%s",
            selector.mode.value,
            _LIFECYCLE_OF_DELETE[selector.mode].value,
            len(affected),
            selector.scope,
        )
        return affected

    async def purge_space(self, org: str, space: str) -> list[str]:
        _ensure_local_scope(Scope(org=org, space=space))
        purged_units: list[MemoryUnit] = []
        for scope in [
            candidate
            for candidate in self._kv.scopes()
            if candidate.org == org and candidate.space == space
        ]:
            units = self._list_units(scope)
            for unit in units:
                self._kv.delete(scope, memory_key(unit.id))
            purged_units.extend(units)
        self._index.remove(purged_units)
        return [unit.id for unit in purged_units]

    async def evolve(
        self, scope: Scope, mode: EvolveMode, channel: Channel = Channel.BACKGROUND
    ) -> str:
        _ensure_local_scope(scope)
        if self._job_factory is None:
            raise RuntimeError(
                "evolve requires job_factory, please configure "
                "engine.default.job_factory"
            )
        job = self._job_factory.get_job(JobType.EVOLVE, scope=scope, mode=mode)
        job_id = await self._scheduler.submit(job, channel)
        logger.info(
            "Engine.evolve submitted: job_id=%s scope=%s mode=%s channel=%s",
            job_id,
            scope,
            mode.value,
            channel.value,
        )
        return job_id

    async def admin_get(self, key: str) -> str:  # 由 API 层直达 PolicyManager
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_set(self, key: str, value: str) -> None:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_all(self) -> dict[str, str]:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")


# -- 注册到 EngineProducer（实现自注册，新增无需改 producer/build_kernel） -------- #






@EngineProducer.register("in_memory")
def _build(config):
    # index_builder 缺省随 vector_enabled 在 hybrid/fulltext 间择一。
    # 与 evolver 一致，共享同一实例。
    ib_default = "hybrid" if config.get("vector_enabled", True) else "fulltext"
    # classifier 可选：config 声明了 classifier 命名空间具名实例则注入，None 时跳过（向后兼容）。
    # infer=false 默认路径用它给原文打 tier+tags；infer=true 由 extractor 产出不经 classifier。

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
    # None 时 evolve/middle 路径报错（向后兼容——纯默认配置不走演进）。
    def _opt_job_factory():
        ctx = config.ctx
        ns = ctx.namespaces.get(JobFactoryProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        import control.jobs_impl as _ji  # noqa: F401
        _ = _ji
        return JobFactoryProducer.build_named("default", ctx)

    middle_interval = int(config.get("middle_interval", 50))

    return InMemoryEngine(
        IngestorProducer.dep(config, default="simple"),
        IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        RetrieverProducer.dep(config, default="pipeline"),
        KvProducer.dep(config, default="memory"),
        SchedulerProducer.dep(config, default="in_process"),
        EvolverProducer.dep(config, default="orchestrating"),
        LifecycleProducer.dep(config, default="kv"),
        classifier=_opt_classifier(),
        pipeline=_opt_pipeline(),
        job_factory=_opt_job_factory(),
        middle_interval=middle_interval,
    )
