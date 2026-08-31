# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""由现有 Store 和 Recaller 组合而成的默认统一 Storage。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from jiuwen_memory.common.errors import (
    NotFoundError,
    StorageRetrievalError,
    UnsupportedStorageCapabilityError,
    ValidationError,
    safe_error_message,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
    CandidateFuser,
    ChannelError,
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    RankedStorageResult,
    RecallBatch,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredMemoryUnit,
    ScoredUnit,
    is_retrieval_candidate,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.fusion import FusionProducer, FusionStore
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.security import (
    AllowAllStorageSecurity,
    StorageAccessContext,
    StorageAction,
    StorageSecurity,
)
from jiuwen_memory.storage.storage import Storage, StorageCapability, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode, MemoryListResult
from jiuwen_memory.storage.vector import VectorProducer, VectorStore


class _AuthorizedStoreProxy:
    """给现有 Store 方法增加可选 access，同时避免暴露原始实例。"""

    def __init__(self, store: Any, security: StorageSecurity, resource: str) -> None:
        self._store = store
        self._security = security
        self._resource = resource

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._store, name)
        if not callable(member):
            return member

        def authorized(*args: Any, **kwargs: Any) -> Any:
            access = kwargs.pop("access", None)
            scope = args[0] if args and isinstance(args[0], Scope) else Scope()
            action = _action_for_store_method(name)
            self._security.authorize(access, scope, action, self._resource)
            return member(*args, **kwargs)

        return authorized


def _action_for_store_method(name: str) -> StorageAction:
    if name == "insert":
        return StorageAction.ADD
    if name == "update":
        return StorageAction.UPDATE
    if name == "delete":
        return StorageAction.DELETE
    if name in {"search", "recall", "seed_ids"}:
        return StorageAction.SEARCH
    if name in {"get", "mget", "exists", "scan", "list", "stat"}:
        return StorageAction.GET
    return StorageAction.ADMIN


class CompositeStorage(Storage):
    def __init__(
        self,
        *,
        kv: KVStore | None = None,
        vector: VectorStore | None = None,
        fulltext: FulltextStore | None = None,
        graph: GraphStore | None = None,
        fusion: FusionStore | None = None,
        fs: FSStore | None = None,
        kv_ports: dict[str, KVStore] | None = None,
        vector_ports: dict[str, VectorStore] | None = None,
        fulltext_ports: dict[str, FulltextStore] | None = None,
        graph_ports: dict[str, GraphStore] | None = None,
        fusion_ports: dict[str, FusionStore] | None = None,
        fs_ports: dict[str, FSStore] | None = None,
        recallers: list[Any] | None = None,
        preferred_pipeline: RetrievalPipeline = RetrievalPipeline.RECALL_GET_RANK,
        security: StorageSecurity | None = None,
    ) -> None:
        self._stores = {
            StorageCapability.KV: kv,
            StorageCapability.VECTOR: vector,
            StorageCapability.FULLTEXT: fulltext,
            StorageCapability.GRAPH: graph,
            StorageCapability.FUSION: fusion,
            StorageCapability.FS: fs,
        }
        self._capabilities = frozenset(
            capability for capability, store in self._stores.items() if store is not None
        )
        configured_ports = {
            StorageCapability.KV: kv_ports,
            StorageCapability.VECTOR: vector_ports,
            StorageCapability.FULLTEXT: fulltext_ports,
            StorageCapability.GRAPH: graph_ports,
            StorageCapability.FUSION: fusion_ports,
            StorageCapability.FS: fs_ports,
        }
        self._named_stores: dict[StorageCapability, dict[str, Any]] = {}
        for capability, store in self._stores.items():
            ports = dict(configured_ports.get(capability) or {})
            if store is not None:
                ports["default"] = store
            self._named_stores[capability] = ports
        self._recallers = list(recallers or [])
        self._preferred_pipeline = preferred_pipeline
        self._security = security or AllowAllStorageSecurity()
        self._proxies = {
            capability: {
                name: _AuthorizedStoreProxy(store, self._security, capability.value)
                for name, store in ports.items()
            }
            for capability, ports in self._named_stores.items()
        }

    @property
    def security(self) -> StorageSecurity:
        return self._security

    @property
    def kv(self) -> KVStore:
        return cast(KVStore, self._port(StorageCapability.KV))

    @property
    def vector(self) -> VectorStore:
        return cast(VectorStore, self._port(StorageCapability.VECTOR))

    @property
    def fulltext(self) -> FulltextStore:
        return cast(FulltextStore, self._port(StorageCapability.FULLTEXT))

    @property
    def graph(self) -> GraphStore:
        return cast(GraphStore, self._port(StorageCapability.GRAPH))

    @property
    def fusion(self) -> FusionStore:
        return cast(FusionStore, self._port(StorageCapability.FUSION))

    @property
    def fs(self) -> FSStore:
        return cast(FSStore, self._port(StorageCapability.FS))

    @staticmethod
    def _validate_units(scope: Scope, units: list[MemoryUnit]) -> None:
        invalid = [unit.id for unit in units if unit.scope != scope]
        if invalid:
            raise ValidationError(f"MemoryUnit scope differs from explicit scope: {invalid}")

    def bind_recallers(self, recallers: list[Any]) -> None:
        """在装配阶段绑定检索适配器；同一 Storage 不允许绑定两套不同实例。"""
        bound = list(recallers)
        same_binding = len(self._recallers) == len(bound) and all(
            current is candidate for current, candidate in zip(self._recallers, bound)
        )
        if self._recallers and not same_binding:
            raise ValidationError("CompositeStorage cannot be rebound to different recallers")
        self._recallers = bound

    def capabilities(self) -> frozenset[StorageCapability]:
        return self._capabilities

    def has_kv_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.KV, name)

    def has_vector_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.VECTOR, name)

    def has_fulltext_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FULLTEXT, name)

    def has_graph_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.GRAPH, name)

    def has_fusion_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FUSION, name)

    def has_fs_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FS, name)

    def kv_port(self, name: str = "default") -> KVStore:
        return cast(KVStore, self._port(StorageCapability.KV, name))

    def vector_port(self, name: str = "default") -> VectorStore:
        return cast(VectorStore, self._port(StorageCapability.VECTOR, name))

    def fulltext_port(self, name: str = "default") -> FulltextStore:
        return cast(FulltextStore, self._port(StorageCapability.FULLTEXT, name))

    def graph_port(self, name: str = "default") -> GraphStore:
        return cast(GraphStore, self._port(StorageCapability.GRAPH, name))

    def fusion_port(self, name: str = "default") -> FusionStore:
        return cast(FusionStore, self._port(StorageCapability.FUSION, name))

    def fs_port(self, name: str = "default") -> FSStore:
        return cast(FSStore, self._port(StorageCapability.FS, name))

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._preferred_pipeline

    def scopes(self) -> list[Scope]:
        return self._raw_kv().scopes()

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.ADD, "memory_unit")
        # 本实现无投影能力，落地范围仅记忆本体：调用方只要检索索引时无事可做。
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        kv = self._raw_kv()
        for unit in units:
            kv.insert(scope, memory_key(unit.id), dumps(unit))

    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        # 本实现落地范围仅记忆本体，FORWARD_ONLY 与 ALL 行为相同（无检索索引可跳过）。
        self._authorize(access, scope, StorageAction.UPDATE, "memory_unit")
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        self._validate_units(scope, units)
        kv = self._raw_kv()
        for unit in units:
            kv.update(scope, memory_key(unit.id), dumps(unit))

    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.DELETE, "memory_unit")
        # 同 add：无检索索引可单独移除，软删除保留本体即无事可做。
        if mode is IndexRemoveMode.SOFT:
            return
        kv = self._raw_kv()
        for unit_id in unit_ids:
            kv.delete(scope, memory_key(unit_id))

    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        self._authorize(access, scope, StorageAction.GET, "memory_unit")
        return self._get_units(scope, unit_ids)

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
        access: StorageAccessContext | None = None,
    ) -> MemoryListResult:
        self._authorize(access, scope, StorageAction.LIST, "memory_unit")
        result = self._raw_kv().list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )
        items: list[MemoryUnit] = []
        for _, raw in result.entries:
            unit = loads(raw)
            if unit is not None:
                items.append(unit)
        return MemoryListResult(items=items, count=result.count)

    def recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredUnit]:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        return self._recall(scope, query, channels=channels, recall_limit=recall_limit)

    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredMemoryUnit]:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        return self._recall_and_get(
            scope, query, channels=channels, recall_limit=recall_limit
        )

    def retrieve(
        self,
        scope: Scope,
        query: ParsedQuery,
        fuser: CandidateFuser,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        rank_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RankedStorageResult:
        self._authorize(access, scope, StorageAction.SEARCH, "memory_unit")
        materialized = self._recall_and_get(
            scope, query, channels=channels, recall_limit=recall_limit
        )
        filtered: list[list[ScoredMemoryUnit]] = []
        for batch in materialized.batches:
            candidates = []
            for candidate in batch.candidates:
                if _passes(candidate.unit, query):
                    candidates.append(candidate)
            filtered.append(candidates)
        ranked = fuser.fuse(query, filtered)[:rank_limit]
        return RankedStorageResult(candidates=ranked, errors=materialized.errors)

    def health(self) -> None:
        self._security.health()
        checked: set[int] = set()
        for ports in self._named_stores.values():
            for store in ports.values():
                if id(store) in checked:
                    continue
                checked.add(id(store))
                store.security.health()
                store.health()

    def _port(self, capability: StorageCapability, name: str = "default") -> Any:
        try:
            return self._proxies[capability][name]
        except KeyError as exc:
            raise UnsupportedStorageCapabilityError(
                f"storage capability is not available: {capability.value}.{name}"
            ) from exc

    def _has_port(self, capability: StorageCapability, name: str) -> bool:
        return name in self._named_stores[capability]

    def _authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        self._security.authorize(access, scope, action, resource)

    def _raw_kv(self) -> KVStore:
        store = self._stores[StorageCapability.KV]
        if store is None:
            self._port(StorageCapability.KV)
        return cast(KVStore, store)

    def _get_units(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        """批量点读真源：按输入顺序返回，缺失 id 省略，重复 id 各自返回。

        ``mget`` 不去重且任一 key 缺失即抛 ``NotFoundError``（见 :meth:`KVStore.mget`），
        故去重与「索引↔真源短暂不一致」的兜底都由本方法承担。
        """
        if not unit_ids:
            return []
        kv = self._raw_kv()
        unique = list(dict.fromkeys(unit_ids))
        try:
            loaded = list(zip(unique, kv.mget(scope, [memory_key(uid) for uid in unique])))
        except NotFoundError:
            loaded = []
            for unit_id in unique:
                try:
                    loaded.append((unit_id, kv.get(scope, memory_key(unit_id))))
                except NotFoundError:
                    continue
        by_id: dict[str, MemoryUnit] = {}
        for unit_id, raw in loaded:
            unit = loads(raw)
            if unit is not None:
                by_id[unit_id] = unit
        return [by_id[unit_id] for unit_id in unit_ids if unit_id in by_id]

    def _recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredUnit]:
        if channels == []:
            raise ValidationError("channels must be omitted or contain at least one channel")
        selected = [
            recaller
            for recaller in self._recallers
            if channels is None or recaller.channel() in channels
        ]
        if not selected:
            return RecallResult()
        batches: list[RecallBatch[ScoredUnit] | None] = [None] * len(selected)
        errors: list[ChannelError] = []
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            futures = {
                executor.submit(recaller.recall, scope, query, recall_limit): (index, recaller)
                for index, recaller in enumerate(selected)
            }
            for future in as_completed(futures):
                index, recaller = futures[future]
                source = _recaller_source(recaller)
                try:
                    candidates = future.result()
                except Exception as exc:
                    errors.append(
                        ChannelError(
                            channel=recaller.channel(),
                            source=source,
                            error_type=type(exc).__name__,
                            message=safe_error_message(exc),
                        )
                    )
                    continue
                batches[index] = RecallBatch(recaller.channel(), source, candidates)
        if errors and len(errors) == len(selected):
            raise StorageRetrievalError(errors)
        successful = [batch for batch in batches if batch is not None]
        return RecallResult(batches=successful, errors=errors)

    def _recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
    ) -> RecallResult[ScoredMemoryUnit]:
        recalled = self._recall(
            scope, query, channels=channels, recall_limit=recall_limit
        )
        unit_ids: list[str] = []
        seen: set[str] = set()
        for batch in recalled.batches:
            for candidate in batch.candidates:
                if candidate.unit_id not in seen:
                    seen.add(candidate.unit_id)
                    unit_ids.append(candidate.unit_id)
        units = {unit.id: unit for unit in self._get_units(scope, unit_ids)}
        batches = []
        errors = list(recalled.errors)
        for batch in recalled.batches:
            candidates = []
            for candidate in batch.candidates:
                unit = units.get(candidate.unit_id)
                if unit is None:
                    errors.append(
                        ChannelError(
                            channel=batch.channel,
                            source=batch.source,
                            error_type="MissingMemoryUnit",
                            message=f"MemoryUnit not found: {candidate.unit_id}",
                        )
                    )
                    continue
                candidates.append(
                    ScoredMemoryUnit(unit, candidate.score, candidate.channel, candidate.evidence)
                )
            batches.append(RecallBatch(batch.channel, batch.source, candidates))
        return RecallResult(batches=batches, errors=errors)


def _passes(unit: MemoryUnit, query: ParsedQuery) -> bool:
    return is_retrieval_candidate(
        unit,
        as_of=query.as_of,
        time_from=query.time_from,
        time_to=query.time_to,
        filters=query.recheck_filters,
        include_archived=query.include_archived,
    )


def _recaller_source(recaller: Any) -> str:
    layer = getattr(recaller, "layer", None)
    if layer:
        return f"{recaller.channel().value}_{layer}"
    return type(recaller).__name__


def _optional_store(producer: type[Factory], config: Any, field: str) -> Any | None:
    """按字段取可选 Store；未声明即 None——不做匿名兜底装配。"""
    if field not in config.params:
        return None
    return producer.dep(config, field)


def _named_ports(producer: type[Factory], config: Any) -> dict[str, Any]:
    namespace = config.ctx.namespaces.get(producer.TOP_NAME, {})
    return {
        name: producer.build_named(name, config.ctx)
        for name in ("layers_l0", "layers_l1")
        if name in namespace
    }


@StorageProducer.register("composite")
def _build(config):
    pipeline_value = config.get(
        "preferred_retrieval_pipeline", RetrievalPipeline.RECALL_GET_RANK.value
    )
    try:
        preferred_pipeline = RetrievalPipeline(pipeline_value)
    except ValueError as exc:
        supported = [pipeline.value for pipeline in RetrievalPipeline]
        raise ValidationError(
            f"Unsupported preferred_retrieval_pipeline {pipeline_value!r}; "
            f"expected one of {supported}"
        ) from exc
    return CompositeStorage(
        kv=KvProducer.dep(config, default="memory"),
        vector=_optional_store(VectorProducer, config, "vector_store"),
        fulltext=_optional_store(FulltextProducer, config, "fulltext_store"),
        graph=_optional_store(GraphProducer, config, "graph_store"),
        fusion=_optional_store(FusionProducer, config, "fusion_store"),
        fs=_optional_store(FsProducer, config, "fs_store"),
        vector_ports=_named_ports(VectorProducer, config),
        fulltext_ports=_named_ports(FulltextProducer, config),
        preferred_pipeline=preferred_pipeline,
    )
