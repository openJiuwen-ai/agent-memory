"""CloudEngine — 面向云侧部署的 profile-aware 读写编排实现。

CloudEngine 直接实现 :class:`control.engine.MemoryEngine`，不继承
``InMemoryEngine``，避免云侧 message_type 路由、安全 KV、scope 一致性校验与本地
最小实现产生隐式耦合。它只编排已装配的 Ingestor / construction / retrieval /
KVStore / control 算子，不在 engine 内拼 prompt、选模型或执行鉴权。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from common.errors import NotFoundError, ValidationError
from common.log import get_logger
from common.type_def import (
    MEMORY_KEY_PREFIX,
    LifecycleState,
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
from control.lifecycle import LifecycleManager, LifecycleProducer
from control.pipeline import MemoryPipeline, PipelineBinding, PipelineProducer
from control.scheduler import Scheduler, SchedulerProducer
from control.types import (
    Channel,
    DeleteMode,
    DeleteSelector,
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
    if patch.metadata is not None:
        new.metadata.update({key: str(value) for key, value in patch.metadata.items()})
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
    raw = unit.metadata.get("importance")
    try:
        value = float(raw) if raw is not None else 1.0
    except ValueError:
        value = 1.0
    unit.metadata["importance"] = f"{max(0.0, value * 0.5):g}"


def _unit_memory_type(unit: MemoryUnit) -> str:
    return str(unit.metadata.get("memory_type", "")).strip() or unit.tier.value


def _unit_sort_key(unit: MemoryUnit) -> tuple[datetime, str]:
    return (unit.temporal.t_ingest or datetime.min.replace(tzinfo=timezone.utc), unit.id)


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


def _truthy(metadata: dict[str, str], key: str) -> bool:
    return metadata.get(key, "").strip().lower() == "true"


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


class CloudEngine(MemoryEngine):
    """云侧读写编排：以 message_type 选择构建/查询 profile。"""

    def __init__(
        self,
        ingestor: Ingestor,
        index_builder: IndexBuilder,
        retriever: Retriever,
        kv: KVStore,
        scheduler: Scheduler,
        evolver: Evolver,
        lifecycle: LifecycleManager,
        *,
        classifier: Classifier | None = None,
        pipeline: MemoryPipeline | None = None,
        message_type_key: str = "message_type",
        default_message_type: str = "chat",
        default_pipeline_name: str = "default",
    ) -> None:
        self._ingestor = ingestor
        self._index = index_builder
        self._retriever = retriever
        self._kv = kv
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
        metadata: dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        meta = self._normalized_metadata(metadata)
        payload = RawPayload(
            id=str(uuid.uuid4()),
            scope=scope,
            modality=source,
            data=content.encode("utf-8"),
            metadata=meta,
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

        procedural = _truthy(meta, "procedural")
        infer = _truthy(meta, "infer")
        if procedural or infer:
            result = evolver.evolve(units, EvolveMode.EXTRACT)
            derived = [self._load(scope, unit_id) for unit_id in result.created_ids]
            logger.info(
                "CloudEngine.write %s: originals=%d derived=%d scope=%s pipeline=%s",
                "procedural=True" if procedural else "infer=True",
                len(units),
                len(derived),
                scope,
                pipeline_name,
            )
            return derived

        if classifier is not None:
            classifier.classify(units)
        for unit in units:
            self._kv.insert(scope, memory_key(unit.id), dumps(unit))
        index_builder.build(units)
        logger.info(
            "CloudEngine.write raw_indexed: units=%d scope=%s message_type=%s pipeline=%s",
            len(units),
            scope,
            meta.get(self._message_type_key, ""),
            pipeline_name,
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
    ) -> list[MemoryUnit]:
        return self._list_page(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
        )

    def _list_page(
        self,
        scope: Scope,
        *,
        offset: int,
        limit: int,
        memory_types: list[str] | None,
    ) -> list[MemoryUnit]:
        if offset < 0:
            raise ValidationError("offset must be >= 0")
        if limit <= 0:
            raise ValidationError("limit must be > 0")
        wanted: set[str] = set()
        for raw_memory_type in memory_types or []:
            memory_type = str(raw_memory_type).strip()
            if memory_type:
                wanted.add(memory_type)
        units = self._list_units(scope)
        if wanted:
            units = [unit for unit in units if _unit_memory_type(unit) in wanted]
        units.sort(key=_unit_sort_key, reverse=True)
        return units[offset: offset + limit]

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
    ) -> tuple[list[MemoryUnit], list[PermissionContext]]:
        units = self._list_page(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
        )
        return units, [_permission_context_from_unit(unit) for unit in units]

    async def permission_contexts_for_delete(
        self, selector: DeleteSelector
    ) -> list[PermissionContext]:
        scopes = [selector.scope] if selector.scope is not None else self._kv.scopes()
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
        new.metadata["pipeline"] = new_pipeline
        new_index = new_binding.index_builder if new_binding is not None else self._index

        if patch.mode == UpdateMode.OVERWRITE:
            new.id = old.id
            self._kv.update(scope, memory_key(new.id), dumps(new))
            old_index = self._index_for_unit(old)
            if old_index is new_index:
                new_index.update([new])
            else:
                old_index.remove([old])
                new_index.build([new])
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
        self._kv.insert(scope, memory_key(new.id), dumps(new))
        old = self._lifecycle.supersede(scope, old.id, new.temporal.t_valid)
        self._update_indexes([old])
        new_index.build([new])
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

        scopes = [selector.scope] if selector.scope is not None else self._kv.scopes()
        if not scopes:
            scopes = [Scope()]

        scanned: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in scopes:
            for key, raw in self._kv.list(scope, MEMORY_KEY_PREFIX):
                unit = loads(raw)
                if unit is None:
                    continue
                self._ensure_unit_scope(unit, scope)
                scanned.append((scope, key, unit))

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
            for scope, key, unit in scanned:
                if _scoped_unit_id(scope, unit.id) in purge_ids:
                    self._kv.delete(scope, key)
                    purged_units.append(unit)
            self._remove_indexes(purged_units)
            return [unit.id for unit in purged_units]

        if selector.mode == DeleteMode.DOWNWEIGHT:
            update_units: list[MemoryUnit] = []
            for scope, key, unit in matches:
                _downweight_importance(unit)
                self._kv.update(scope, key, dumps(unit))
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
        self._remove_indexes([unit for _, _, unit in matches])
        return affected

    async def purge_space(self, org: str, space: str) -> list[str]:
        purged: list[str] = []
        for scope in [
            candidate
            for candidate in self._kv.scopes()
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
        job_id = self._scheduler.submit(scope, mode, channel)
        logger.info(
            "CloudEngine.evolve delegated to scheduler: job_id=%s scope=%s mode=%s channel=%s",
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

    def _normalized_metadata(self, metadata: dict[str, str] | None) -> dict[str, str]:
        meta = {key: str(value) for key, value in (metadata or {}).items()}
        message_type = meta.get(self._message_type_key, "").strip() or self._default_message_type
        if message_type:
            meta[self._message_type_key] = message_type
        return meta

    def _normalize_unit_metadata(self, unit: MemoryUnit) -> None:
        meta = {key: str(value) for key, value in unit.metadata.items()}
        message_type = meta.get(self._message_type_key, "").strip() or self._default_message_type
        if message_type:
            meta[self._message_type_key] = message_type
        unit.metadata = meta

    def _normalized_query(self, query: RetrievalQuery) -> RetrievalQuery:
        value = str(query.extensions.get(self._message_type_key, "")).strip()
        if value or not self._default_message_type:
            return query
        routed = copy.deepcopy(query)
        routed.extensions[self._message_type_key] = self._default_message_type
        return routed

    def _prepare_ingested_units(
        self,
        units: list[MemoryUnit],
        scope: Scope,
        metadata: dict[str, str],
        *,
        assets: list[str] | None,
        tags: list[str] | None,
    ) -> None:
        for unit in units:
            self._ensure_unit_scope(unit, scope)
            unit.metadata.update(metadata)
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
            unit.metadata["pipeline"] = pipeline_name

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

    def _remove_indexes(self, units: list[MemoryUnit]) -> None:
        for group in self._group_by_index(units):
            group.builder.remove(group.units)

    def _update_indexes(self, units: list[MemoryUnit]) -> None:
        for group in self._group_by_index(units):
            group.builder.update(group.units)

    def _load(self, scope: Scope, unit_id: str) -> MemoryUnit:
        try:
            raw = self._kv.get(scope, memory_key(unit_id))
        except NotFoundError:
            raise NotFoundError("memory_unit", unit_id) from None
        unit = loads(raw)
        if unit is None:
            raise NotFoundError("memory_unit", unit_id)
        self._ensure_unit_scope(unit, scope)
        return unit

    def _list_units(self, scope: Scope) -> list[MemoryUnit]:
        units: list[MemoryUnit] = []
        for _, raw in self._kv.list(scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None:
                continue
            self._ensure_unit_scope(unit, scope)
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


@EngineProducer.register("cloud")
def _build(config):
    ib_default = "hybrid" if config.get("vector_enabled", True) else "fulltext"
    return CloudEngine(
        IngestorProducer.dep(config, default="simple"),
        IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        RetrieverProducer.dep(config, default="pipeline"),
        KvProducer.dep(config, default="memory"),
        SchedulerProducer.dep(config, default="in_process"),
        EvolverProducer.dep(config, default="orchestrating"),
        LifecycleProducer.dep(config, default="kv"),
        classifier=_optional_classifier(config),
        pipeline=_optional_pipeline(config),
        message_type_key=str(config.get("message_type_key", "message_type")),
        default_message_type=str(config.get("default_message_type", "chat")),
        default_pipeline_name=str(config.get("default_pipeline_name", "default")),
    )
