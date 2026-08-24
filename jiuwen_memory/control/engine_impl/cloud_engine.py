"""CloudEngine — 面向云侧部署的 profile-aware 读写编排实现。

CloudEngine 直接实现 :class:`control.engine.MemoryEngine`，不继承
``InMemoryEngine``，避免云侧 message_type 路由、安全 KV、scope 一致性校验与本地
最小实现产生隐式耦合。它只编排已装配的 Ingestor / construction / retrieval /
KVStore / control 算子，不在 engine 内拼 prompt、选模型或执行鉴权。
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jiuwen_memory.common.errors import AgentMemoryError, NotFoundError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    FilterExpr,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    MetadataValueType,
    Modality,
    RawPayload,
    Scope,
    Segment,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.classifier import Classifier, ClassifierProducer
from jiuwen_memory.construction.evolver import Evolver, EvolverProducer
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine import EngineProducer, MemoryEngine
from jiuwen_memory.control.engine_impl.list_support import list_page
from jiuwen_memory.control.jobs import JobFactory, JobFactoryProducer, JobType
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
    UpdateMode,
)
from jiuwen_memory.ingest.ingestor import Ingestor, IngestorProducer
from jiuwen_memory.retrieval.retriever import Retriever, RetrieverProducer
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)
_LIFECYCLE_OF_DELETE = {
    DeleteMode.FORGET: LifecycleState.FORGOTTEN,
    DeleteMode.ARCHIVE: LifecycleState.ARCHIVED,
    DeleteMode.DOWNWEIGHT: LifecycleState.ACTIVE,
}


@dataclass
class _IndexGroup:
    builder: IndexBuilder
    units: list[MemoryUnit]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_patch(old: MemoryUnit, patch: MemoryPatch) -> MemoryUnit:
    new = copy.deepcopy(old)
    if patch.content is not None:
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
    raw = unit.system_metadata.get("importance")
    try:
        value = float(raw) if raw is not None else 1.0
    except ValueError:
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


def _truthy(metadata: dict[str, MetadataValueType], key: str) -> bool:
    return str(metadata.get(key, "")).strip().lower() == "true"


def _matches_delete_selector(unit: MemoryUnit, selector: DeleteSelector) -> bool:
    wanted_ids = set(selector.unit_ids)
    wanted_tags = set(selector.tags)
    if wanted_ids and unit.id not in wanted_ids:
        return False
    if wanted_tags and not wanted_tags.intersection(unit.tags):
        return False
    if selector.before is not None:
        t_message = unit.temporal.t_message
        if t_message is None or t_message >= selector.before:
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


class CloudEngine(MemoryEngine):
    """云侧读写编排：以 message_type 选择构建/查询 profile。"""

    def __init__(
        self,
        ingestor: Ingestor,
        index_builder: IndexBuilder,
        retriever: Retriever,
        storage: Storage,
        scheduler: Scheduler,
        evolver: Evolver,
        lifecycle: LifecycleManager,
        *,
        classifier: Classifier | None = None,
        pipeline: MemoryPipeline | None = None,
        message_type_key: str = "message_type",
        default_message_type: str = "chat",
        default_pipeline_name: str = "default",
        job_factory: JobFactory | None = None,
    ) -> None:
        self._ingestor = ingestor
        self._index = index_builder
        self._retriever = retriever
        self._storage = storage
        self._scheduler = scheduler
        self._evolver = evolver
        self._lifecycle = lifecycle
        self._classifier = classifier
        self._pipeline = pipeline
        self._message_type_key = message_type_key.strip()
        if not self._message_type_key:
            raise ValidationError("CloudEngine message_type_key must not be empty")
        self._default_message_type = default_message_type.strip()
        self._default_pipeline_name = default_pipeline_name.strip()
        self._job_factory = job_factory

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.ENGINE

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
        raw_meta = dict(system_metadata or {})
        procedural = _truthy(raw_meta, "procedural")
        infer = _truthy(raw_meta, "infer")
        middle = _truthy(raw_meta, "middle")  # 二级开关（仅在 infer=true 下生效）
        if middle and not infer and not procedural:
            raise ValueError(
                "metadata.middle=true requires infer=true (middle 是 infer 下的二级开关)"
            )

        middle_interval = raw_meta.pop("middle_interval", None)
        raw_meta.pop("middle", None)

        meta = self._normalized_metadata(raw_meta)
        is_video = source == Modality.VIDEO
        payload_id = str(meta.get("payload_id", "")).strip() if is_video else ""
        payload = RawPayload(
            id=payload_id or str(uuid.uuid4()),
            scope=scope,
            modality=source,
            data=b"" if is_video else content.encode("utf-8"),
            uri=content if is_video else "",
            system_metadata=meta,
            user_metadata=dict(user_metadata or {}),
            occurred_at=occurred_at,
        )
        units = self._ingestor.ingest([payload])
        self._prepare_ingested_units(units, scope, meta, assets=assets, tags=tags)

        binding = self._write_binding(units)
        pipeline_name = binding.name if binding is not None else self._default_pipeline_name
        self._stamp_pipeline(units, pipeline_name)

        evolver = binding.evolver if binding is not None else self._evolver
        index_builder = binding.index_builder if binding is not None else self._index
        classifier = binding.classifier if binding is not None else self._classifier

        # procedural 优先（与 InMemoryEngine 三路分流一致）
        if procedural:
            if evolver is None:
                raise RuntimeError(
                    "CloudEngine.write procedural=True requires an Evolver (装配未注入 evolver)"
                )
            result = await asyncio.to_thread(
                evolver.evolve, units, EvolveMode.EXTRACT
            )
            derived = [self._load(scope, unit_id) for unit_id in result.created_ids]
            logger.info(
                "CloudEngine.write procedural=True: originals=%d derived=%d scope=%s pipeline=%s",
                len(units),
                len(derived),
                scope,
                pipeline_name,
            )
            return derived

        # infer=true 下按 middle 二级分流
        if infer:
            if middle:
                return await self._write_middle_path(
                    units, scope, index_builder, evolver, pipeline_name,
                    middle_interval=middle_interval,
                )
            # 既有同步抽取路径，不动
            if evolver is None:
                raise RuntimeError(
                    "CloudEngine.write infer=True requires an Evolver (装配未注入 evolver)"
                )
            result = await asyncio.to_thread(
                evolver.evolve, units, EvolveMode.EXTRACT
            )
            derived = [self._load(scope, unit_id) for unit_id in result.created_ids]
            logger.info(
                "CloudEngine.write infer=True: originals=%d derived=%d scope=%s pipeline=%s",
                len(units),
                len(derived),
                scope,
                pipeline_name,
            )
            return derived

        # 默认路径（infer=false）：classifier 给原文打 tier+tags → 落 /memory/{id} + 建索引
        if classifier is not None:
            classifier.classify(units)
        # 记忆写入只经 IndexBuilder：交付 Storage + 建索引由其统一编排。
        await asyncio.to_thread(index_builder.build, units)
        logger.info(
            "CloudEngine.write raw_indexed: units=%d scope=%s message_type=%s pipeline=%s",
            len(units),
            scope,
            meta.get(self._message_type_key, ""),
            pipeline_name,
        )
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

    # ---- 中期缓冲子路径 ----

    async def _write_middle_path(
        self,
        units: list[MemoryUnit],
        scope: Scope,
        index_builder: IndexBuilder,
        evolver: Evolver,
        pipeline_name: str,
        *,
        middle_interval: Any,
    ) -> list[MemoryUnit]:
        """中期缓冲子路径：原文落 /memory/ + 建索引 + tier=WORKING + 提交定时 MiddleToLongJob。

        多 profile 适配：此处通过 ``get_job`` 的运行时覆盖入参 ``evolver=`` / ``index=``
        注入 binding 选的——保证 Job 内部 evolver/index 与原文落盘时一致（否则原文用
        chat_index 建索引但归档调 default_index.remove，索引不会被正确清理）。

        ``middle_interval`` 经 write 的 ``system_metadata`` 透传，但不落盘到生成的
        ``MemoryUnit.system_metadata``；``None`` 时由 Spec 装配期默认兜底。
        """
        if self._job_factory is None:
            raise RuntimeError(
                "middle path requires job_factory, please configure "
                "engine.default.job_factory"
            )
        if evolver is None:
            raise RuntimeError(
                "CloudEngine.write middle=true requires an Evolver (装配未注入 evolver)"
            )

        for unit in units:
            unit.tier = MemoryTier.WORKING
            unit.system_metadata["middle"] = "true"
        await asyncio.to_thread(index_builder.build, units)

        # 通过 JobFactory.get_job 的运行时覆盖入参注入 binding 的 evolver/index，
        # 保证归档时 index.remove 用对正确的 index。
        job = self._job_factory.get_job(
            JobType.MIDDLE_TO_LONG,
            scope=scope,
            evolver=evolver,
            index=index_builder,
            interval=middle_interval,
        )
        await self._scheduler.submit(job, channel=Channel.BACKGROUND)
        logger.info(
            "CloudEngine.write middle=True: %d originals buffered, scope=%s "
            "pipeline=%s interval=%s",
            len(units),
            scope,
            pipeline_name,
            middle_interval,
        )
        return units

    async def recall(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        routed_query = self._normalized_query(query)
        binding = self._recall_binding(routed_query)
        retriever = binding.retriever if binding is not None else self._retriever
        return retriever.retrieve(scope, routed_query)

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
        return list_page(
            self._storage,
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
        return _permission_context_from_unit(self._load(scope, unit_id))

    async def list_with_permission_contexts(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
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
        scopes = [selector.scope] if selector.scope is not None else self._storage.scopes()
        if not scopes:
            scopes = [Scope()]
        contexts: list[PermissionContext] = []
        for scope in scopes:
            for unit in self._list_units(scope):
                if _matches_delete_selector(unit, selector):
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
            if _valid_at(unit, as_of):
                candidates.append(unit)
        if not candidates:
            raise NotFoundError("memory_unit", unit_id)
        return max(candidates, key=_valid_sort_key)

    async def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch
    ) -> MemoryUnit:
        old = self._load(scope, unit_id)
        new = _apply_patch(old, patch)
        self._normalize_unit_metadata(new)
        new_binding = self._write_binding([new])
        new_pipeline = new_binding.name if new_binding is not None else self._default_pipeline_name
        new.system_metadata["pipeline"] = new_pipeline
        new_index = new_binding.index_builder if new_binding is not None else self._index

        if patch.mode == UpdateMode.OVERWRITE:
            new.id = old.id
            old_index = self._index_for_unit(old)
            if old_index is new_index:
                new_index.update([new])
            else:
                # 跨 pipeline 迁移：记忆本体就地更新（id 不变），检索索引从旧承载者换到
                # 新承载者。本体全程在位——不删不重建，中途失败也不会留下指向空本体的
                # 孤儿检索索引（而那种孤儿无法经 delete 清理：删除路径的扫描源正是正排）。
                # 注：unified 装配下 RETRIEVAL_ONLY/SOFT 调用都会退化为空操作——
                # 一体化后端的 pipeline 绑定本就不改变存储拓扑。
                new_index.update([new], mode=IndexWriteMode.FORWARD_ONLY)
                old_index.remove([old], mode=IndexRemoveMode.SOFT)
                new_index.build([new], mode=IndexWriteMode.RETRIEVAL_ONLY)
            logger.info(
                "CloudEngine.update overwrite: unit_id=%s scope=%s pipeline=%s",
                new.id,
                scope,
                new_pipeline,
            )
            return new

        new.id = str(uuid.uuid4())
        new.supersedes = old.id
        new.lifecycle = LifecycleState.ACTIVE
        if patch.t_valid is None:
            new.temporal.t_valid = _now()
        # 新版先落地再废旧版：任何时刻都有一个可读版本（同 InMemoryEngine）。
        new_index.build([new])
        old = self._lifecycle.supersede(scope, old.id, new.temporal.t_valid)
        self._update_indexes([old])
        logger.info(
            "CloudEngine.update supersede: old_id=%s new_id=%s scope=%s pipeline=%s",
            old.id,
            new.id,
            scope,
            new_pipeline,
        )
        return new

    async def delete(self, selector: DeleteSelector) -> list[str]:
        selector_is_empty = (
            not selector.unit_ids and not selector.tags and selector.before is None
        )
        if selector_is_empty:
            raise ValidationError("DeleteSelector requires unit_ids, tags, or before")

        scopes = [selector.scope] if selector.scope is not None else self._storage.scopes()
        if not scopes:
            scopes = [Scope()]

        scanned: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in scopes:
            for unit in self._list_units(scope):
                self._ensure_unit_scope(unit, scope)
                scanned.append((scope, unit.id, unit))

        matches = [
            (scope, key, unit)
            for scope, key, unit in scanned
            if _matches_delete_selector(unit, selector)
        ]
        affected = [unit.id for _, _, unit in matches]
        if not affected:
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
            # 物理删除：记忆本体与派生索引由各 pipeline 的 IndexBuilder 一并移除。
            self._remove_indexes(purged_units)
            return [unit.id for unit in purged_units]

        if selector.mode == DeleteMode.DOWNWEIGHT:
            update_units: list[MemoryUnit] = []
            for scope, _, unit in matches:
                _downweight_importance(unit)
                update_units.append(unit)
            self._update_indexes(update_units)
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
        # 非破坏式：lifecycle 已把真源改为 ARCHIVED/FORGOTTEN 并保留，
        # 此处仅让检索索引退出检索。
        self._remove_indexes([unit for _, _, unit in matches], mode=IndexRemoveMode.SOFT)
        return affected

    async def purge_space(self, org: str, space: str) -> list[str]:
        purged: list[str] = []
        for scope in [
            candidate
            for candidate in self._storage.scopes()
            if candidate.org == org and candidate.space == space
        ]:
            units = self._list_units(scope)
            if not units:
                continue
            purged.extend(
                await self.delete(
                    DeleteSelector(
                        unit_ids=[unit.id for unit in units],
                        scope=scope,
                        mode=DeleteMode.PURGE,
                    )
                )
            )
        return purged

    async def evolve(
        self, scope: Scope, mode: EvolveMode, channel: Channel = Channel.BACKGROUND
    ) -> str:
        """提交 EvolveJob 到 Scheduler——mode 经构造参数流入 EvolveJob（运行时参数，不进 Spec）。"""
        if self._job_factory is None:
            raise RuntimeError(
                "evolve requires job_factory, please configure "
                "engine.default.job_factory"
            )
        job = self._job_factory.get_job(JobType.EVOLVE, scope=scope, mode=mode)
        job_id = await self._scheduler.submit(job, channel)
        logger.info(
            "CloudEngine.evolve submitted: job_id=%s scope=%s mode=%s channel=%s",
            job_id,
            scope,
            mode.value,
            channel.value,
        )
        return job_id

    async def admin_get(self, key: str) -> str:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_set(self, key: str, value: str) -> None:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    async def admin_all(self) -> dict[str, str]:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")

    def _write_middle_to_kv(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """``asyncio.to_thread`` 只接 callable + args，抽成同步方法以便包装。"""
        self._storage.add(scope, units)

    def _write_default_to_kv(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """``asyncio.to_thread`` 只接 callable + args，抽成同步方法以便包装。"""
        self._storage.add(scope, units)

    def _normalized_metadata(
        self, metadata: dict[str, MetadataValueType] | None
    ) -> dict[str, MetadataValueType]:
        meta = dict(metadata or {})
        message_type = (
            str(meta.get(self._message_type_key, "")).strip()
            or self._default_message_type
        )
        if message_type:
            meta[self._message_type_key] = message_type
        return meta

    def _normalize_unit_metadata(self, unit: MemoryUnit) -> None:
        meta = dict(unit.system_metadata)
        message_type = (
            str(meta.get(self._message_type_key, "")).strip()
            or self._default_message_type
        )
        if message_type:
            meta[self._message_type_key] = message_type
        unit.system_metadata = meta

    def _normalized_query(self, query: RetrievalQuery) -> RetrievalQuery:
        value = str(query.extensions.get(self._message_type_key, "")).strip()
        if value or not self._default_message_type:
            return query
        # 仅为路由默认值创建浅副本。extensions 允许承载调用方注入的运行时对象，
        # 例如 db_query_service / encryption_port；深拷贝会破坏对象身份或直接失败。
        routed = copy.copy(query)
        routed.extensions = {
            **query.extensions,
            self._message_type_key: self._default_message_type,
        }
        return routed

    def _prepare_ingested_units(
        self,
        units: list[MemoryUnit],
        scope: Scope,
        system_metadata: dict[str, MetadataValueType],
        *,
        assets: list[str] | None,
        tags: list[str] | None,
    ) -> None:
        for unit in units:
            self._ensure_unit_scope(unit, scope)
            unit.system_metadata.update(system_metadata)
            if assets:
                if not unit.segments:
                    unit.segments = [Segment(assets=list(assets), source=unit.source)]
                else:
                    unit.segments[0].assets = list(assets)
            unit.tags = list(tags or [])

    def _stamp_pipeline(self, units: list[MemoryUnit], pipeline_name: str) -> None:
        if not pipeline_name:
            return
        for unit in units:
            unit.system_metadata["pipeline"] = pipeline_name

    def _write_binding(self, units: list[MemoryUnit]) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_write(units)

    def _recall_binding(self, query: RetrievalQuery) -> PipelineBinding | None:
        if self._pipeline is None:
            return None
        return self._pipeline.select_for_recall(query)

    def _index_for_unit(self, unit: MemoryUnit) -> IndexBuilder:
        binding = self._write_binding([unit])
        return binding.index_builder if binding is not None else self._index

    def _group_by_index(self, units: list[MemoryUnit]) -> list[_IndexGroup]:
        groups: dict[int, _IndexGroup] = {}
        for unit in units:
            builder = self._index_for_unit(unit)
            marker = id(builder)
            if marker not in groups:
                groups[marker] = _IndexGroup(builder=builder, units=[])
            groups[marker].units.append(unit)
        return list(groups.values())

    def _remove_indexes(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        for group in self._group_by_index(units):
            group.builder.remove(group.units, mode=mode)

    def _update_indexes(self, units: list[MemoryUnit]) -> None:
        for group in self._group_by_index(units):
            group.builder.update(group.units)

    def _load(self, scope: Scope, unit_id: str) -> MemoryUnit:
        units = self._storage.get(scope, [unit_id])
        if not units:
            raise NotFoundError("memory_unit", unit_id)
        unit = units[0]
        self._ensure_unit_scope(unit, scope)
        return unit

    def _list_units(self, scope: Scope) -> list[MemoryUnit]:
        units = self._storage.list(scope, limit=1_000_000).items
        for unit in units:
            self._ensure_unit_scope(unit, scope)
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

    def _ensure_unit_scope(self, unit: MemoryUnit, scope: Scope) -> None:
        if unit.scope != scope:
            raise ValidationError(
                f"memory unit {unit.id!r} scope mismatch: expected {scope!r}, got {unit.scope!r}"
            )


def _optional_classifier(config) -> Classifier | None:
    if ClassifierProducer.TOP_NAME in config.params:
        return ClassifierProducer.dep(config)
    ns = config.ctx.namespaces.get(ClassifierProducer.TOP_NAME, {})
    if "default" not in ns:
        return None
    return ClassifierProducer.build_named("default", config.ctx)


def _optional_pipeline(config) -> MemoryPipeline | None:
    if PipelineProducer.TOP_NAME in config.params:
        return PipelineProducer.dep(config)
    ns = config.ctx.namespaces.get(PipelineProducer.TOP_NAME, {})
    if "default" not in ns:
        return None
    return PipelineProducer.build_named("default", config.ctx)


def _optional_job_factory(config) -> JobFactory | None:
    """与 InMemoryEngine._opt_job_factory 一致——具名实例则注入，None 时
    evolve/middle 路径报错（向后兼容）。"""
    ctx = config.ctx
    ns = ctx.namespaces.get(JobFactoryProducer.TOP_NAME, {})
    if "default" not in ns:
        return None
    import jiuwen_memory.control.jobs_impl as _ji  # noqa: F401
    _ = _ji
    return JobFactoryProducer.build_named("default", ctx)


@EngineProducer.register("cloud")
def _build(config):
    ib_default = "hybrid" if config.get("vector_enabled", True) else "fulltext"
    return CloudEngine(
        IngestorProducer.dep(config, default="simple"),
        IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        RetrieverProducer.dep(config, default="pipeline"),
        StorageProducer.resolve(config),
        SchedulerProducer.dep(config, default="in_process"),
        EvolverProducer.dep(config, default="orchestrating"),
        LifecycleProducer.dep(config, default="kv"),
        classifier=_optional_classifier(config),
        pipeline=_optional_pipeline(config),
        message_type_key=str(config.get("message_type_key", "message_type")),
        default_message_type=str(config.get("default_message_type", "chat")),
        default_pipeline_name=str(config.get("default_pipeline_name", "default")),
        job_factory=_optional_job_factory(config),
    )
