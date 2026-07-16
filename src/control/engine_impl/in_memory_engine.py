"""最小实现：:class:`~control.engine.MemoryEngine`（接口层各语义的编排中枢）。

串起 write→recall→get（及 update/delete/evolve），按架构 §14 Write 路径：
规约（Ingestor）→ 真源落盘（UnitStore）→ hot 索引（IndexBuilder）→ 提交
background 演进（Scheduler）。接入/构建/检索算子与真源 Store 由装配注入；
``admin_*`` 在本最小实现里不落引擎（由 API 层直达 PolicyManager）。
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Dict, List

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
from control.scheduler import Scheduler, SchedulerProducer
from control.types import (
    Channel,
    DeleteMode,
    DeleteSelector,
    MemoryPatch,
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
    if valid_from is not None and invalid_from is not None and invalid_from <= valid_from:
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
    units: list[tuple[Scope, str, MemoryUnit]], seed_ids: set[str]
) -> set[str]:
    purge_ids = set(seed_ids)
    changed = True
    while changed:
        changed = False
        for _, _, unit in units:
            if unit.id in purge_ids:
                continue
            if any(parent_id in purge_ids for parent_id in unit.provenance):
                purge_ids.add(unit.id)
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
    ) -> None:
        self._ingestor = ingestor
        self._index = index_builder
        self._retriever = retriever
        self._kv = kv  # 真源：裸 kv 存序列化字节
        self._scheduler = scheduler
        self._evolver = evolver
        self._lifecycle = lifecycle
        # classifier 可选：infer=false（默认路径）时给原文打 tier+tags；
        # None 时跳过（原文 tier 保持 EPISODIC 默认，向后兼容）。
        self._classifier = classifier

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
        assets: List[str] | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> List[MemoryUnit]:
        # 调用级开关（经 metadata 下推，对齐 mem0 add(infer=True)）：
        # - procedural=true：过程记忆抽取——原文不落 KV，evolver 让 extractor 把本轮汇总成
        #   1 条 PROCEDURAL 执行历史，落 /memory/ 建索引；不走去重、不收集 context。
        # - infer=true：同步抽取——原文落 /messages/（不建索引），evolver 收集最近10条原文
        #   （指代消解/语境）+ 召回10条相关记忆（去重提示），调 extractor 抽派生落 /memory/。
        # - 缺省：原始落 /memory/ + 建索引，不自动提交演进（由调用方显式 evolve() 触发）。
        meta = {k: str(v) for k, v in (metadata or {}).items()}

        def _is_true(key: str) -> bool:
            return meta.get(key, "").strip().lower() == "true"

        procedural = _is_true("procedural")
        infer = _is_true("infer")

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

        if procedural or infer:
            # 同步抽取路径（procedural / infer 共用）：原文不进默认落盘，直接喂 Evolver(EXTRACT)。
            # evolver 据单元 metadata 区分模式：
            # - procedural：原文不落 KV，产 1 条 PROCEDURAL 落 /memory/，跳过 context/dedup。
            # - infer：原文落 /messages/（不建索引，供后续轮指代消解/语境）+ 收集 context + 走 dedup，
            #   派生落 /memory/。evolver 还负责 /messages/ 的最近 10 条淘汰。
            # procedural 与 infer 互斥，若两者同传按 procedural 语义（原文不落）。
            if self._evolver is None:
                raise RuntimeError(
                    "Engine.write procedural/infer=True requires an Evolver (装配未注入 evolver)"
                )
            result = self._evolver.evolve(units, EvolveMode.EXTRACT)
            derived = [self._load(scope, uid) for uid in result.created_ids]
            logger.info(
                "Engine.write %s: %d originals, %d derived added, scope=%s",
                "procedural=True" if procedural else "infer=True",
                len(units),
                len(derived), scope,
            )
            return derived

        # 默认路径（infer=false）：classifier 给原文打 tier+tags → 落 /memory/{id} + 建索引。
        # classifier 为 None 时跳过（tier 保持 EPISODIC 默认，向后兼容）。
        if self._classifier is not None:
            self._classifier.classify(units)
        for unit in units:
            self._kv.insert(scope, memory_key(unit.id), dumps(unit))
        self._index.build(units)                          # hot 轻量索引
        return units

    async def recall(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        return self._retriever.retrieve(scope, query)

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

    def _list_units(self, scope: Scope) -> List[MemoryUnit]:
        # 只列建索引记忆（/memory/ 前缀）。loads 对非 MemoryUnit 记录返回 None，自然过滤。
        # 版本链（SUPERSEDE/supersedes）只在建索引记忆间；原文 /messages/ 无版本链。
        return [
            u for u in (loads(raw) for _, raw in self._kv.list(scope, prefix=MEMORY_KEY_PREFIX))
            if u is not None
        ]

    def _version_family(self, scope: Scope, unit_id: str) -> List[MemoryUnit]:
        units_by_id = {unit.id: unit for unit in self._list_units(scope)}
        if unit_id not in units_by_id:
            raise NotFoundError("memory_unit", unit_id)

        neighbors: Dict[str, set[str]] = {uid: set() for uid in units_by_id}
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
        if as_of is None:
            return self._load(scope, unit_id)

        candidates = [
            unit
            for unit in self._version_family(scope, unit_id)
            if unit.lifecycle != LifecycleState.FORGOTTEN and _valid_at(unit, as_of)
        ]
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
            old = self._lifecycle.supersede(old.id, new.temporal.t_valid)
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

    async def delete(self, selector: DeleteSelector) -> List[str]:
        if not selector.unit_ids and not selector.tags and selector.before is None:
            logger.warning("Engine.delete rejected empty selector")
            raise ValidationError("DeleteSelector requires unit_ids, tags, or before")

        scopes = [selector.scope] if selector.scope is not None else self._kv.scopes()
        if not scopes:
            scopes = [Scope()]
        scanned: list[tuple[Scope, str, MemoryUnit]] = []
        for scope in scopes:
            for unit_id, raw in self._kv.list(scope):
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
            purge_ids = _expand_provenance_descendants(scanned, set(affected))
            purged: List[str] = []
            for scope, unit_id, unit in scanned:
                if unit.id in purge_ids:
                    self._kv.delete(scope, unit_id)
                    purged.append(unit.id)
            self._index.remove(purged)
            logger.info("Engine.delete purge: count=%d scope=%s", len(purged), selector.scope)
            return purged

        if selector.mode == DeleteMode.DOWNWEIGHT:
            update_index: List[MemoryUnit] = []
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

        self._lifecycle.transition(affected, _LIFECYCLE_OF_DELETE[selector.mode])
        self._index.remove(affected)
        logger.info(
            "Engine.delete transition: mode=%s target=%s count=%d scope=%s",
            selector.mode.value,
            _LIFECYCLE_OF_DELETE[selector.mode].value,
            len(affected),
            selector.scope,
        )
        return affected

    async def evolve(
        self, scope: Scope, mode: EvolveMode, channel: Channel = Channel.BACKGROUND
    ) -> str:
        job_id = self._scheduler.submit(scope, mode, channel)
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

    async def admin_all(self) -> Dict[str, str]:
        raise NotImplementedError("admin 经 API 层直达 PolicyManager")


# -- 注册到 EngineProducer（实现自注册，新增无需改 producer/build_kernel） -------- #






@EngineProducer.register("in_memory")
def _build(config):
    # index_builder 缺省随 vector_enabled 在 hybrid/fulltext 间择一（与 evolver 一致，共享同一实例）。
    ib_default = "hybrid" if config.get("vector_enabled", True) else "fulltext"
    # classifier 可选：config 声明了 classifier 命名空间具名实例则注入，None 时跳过（向后兼容）。
    # infer=false 默认路径用它给原文打 tier+tags；infer=true 由 extractor 产出不经 classifier。

    def _opt_classifier():
        ctx = config.ctx
        ns = ctx.namespaces.get(ClassifierProducer.TOP_NAME, {})
        if "default" not in ns:
            return None
        return ClassifierProducer.build_named("default", ctx)

    return InMemoryEngine(
        IngestorProducer.dep(config, default="simple"),
        IndexBuilderProducer.dep(config, "index_builder", default=ib_default),
        RetrieverProducer.dep(config, default="pipeline"),
        KvProducer.dep(config, default="memory"),
        SchedulerProducer.dep(config, default="in_process"),
        EvolverProducer.dep(config, default="orchestrating"),
        LifecycleProducer.dep(config, default="kv"),
        classifier=_opt_classifier(),
    )
