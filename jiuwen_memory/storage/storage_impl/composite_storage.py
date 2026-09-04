# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""由现有 Store 和 Recaller 组合而成的默认统一 Storage。

召回路（Recaller）的装配归本实现：工厂按 ``vector_enabled`` / ``graph_enabled`` /
``layers_index_enabled`` 与 ``*_recaller`` 配置在构建期同步组装，装配错误 fail-fast。
recaller builder 会经 ``StorageProducer.resolve`` 回取本 Storage 实例，故工厂先把
构建中的实例预注册进具名缓存再组装召回路，打破循环依赖：具名构建用 ``config.name``
预注册（recaller 命名空间下具名实例的 ``storage`` 引用走 ``build_named`` 命中缓存）；
匿名构建无缓存键，用合成名（``id(storage)`` 唯一）预注册并把 storage 引用注入
recaller params，让 builder 内的 ``resolve`` 走 ``cls.dep`` 第一分支命中合成名缓存。
"""

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
from jiuwen_memory.storage.entity_store import (
    EntityStore,
    EntityStoreProducer,
    adapt_entity_store,
)
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.fusion import FusionProducer, FusionStore
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.raw import KVRawDataStore, RawDataStore, adapt_raw_data_store
from jiuwen_memory.storage.security import (
    AllowAllStorageSecurity,
    StorageAccessContext,
    StorageAction,
    StorageSecurity,
)
from jiuwen_memory.storage.storage import Storage, StorageCapability, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode, MemoryListResult
from jiuwen_memory.storage.vector import VectorProducer, VectorStore


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


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
            scope = _scope_for_store_call(args, kwargs)
            for action in _actions_for_store_method(name, args, kwargs):
                self._security.authorize(access, scope, action, self._resource)
            return member(*args, **kwargs)

        return authorized


def _scope_for_store_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Scope:
    """Resolve the explicit Store scope for positional and keyword calls."""
    if args and isinstance(args[0], Scope):
        return args[0]
    for name in ("scope", "target_scope"):
        scope = kwargs.get(name)
        if isinstance(scope, Scope):
            return scope
    return Scope()


def _actions_for_store_method(
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[StorageAction, ...]:
    """Map one port call to every Storage action it can mutate."""
    if name != "execute_operations":
        return (_action_for_store_method(name),)

    operations = args[1] if len(args) > 1 else kwargs.get("operations", ())
    action_by_operation = {
        "INSERT": StorageAction.ADD,
        "LINK": StorageAction.UPDATE,
        "UNLINK_UPDATE": StorageAction.UPDATE,
        "DELETE": StorageAction.DELETE,
    }
    actions = {
        action_by_operation.get(
            str(getattr(getattr(operation, "type", None), "value", "")),
            StorageAction.ADMIN,
        )
        for operation in operations
    }
    return tuple(
        action
        for action in (
            StorageAction.ADD,
            StorageAction.UPDATE,
            StorageAction.DELETE,
            StorageAction.ADMIN,
        )
        if action in actions
    )


def _action_for_store_method(name: str) -> StorageAction:
    if name == "insert":
        return StorageAction.ADD
    if name == "update":
        return StorageAction.UPDATE
    if name == "delete":
        return StorageAction.DELETE
    if name == "append_raw":
        return StorageAction.ADD
    if name == "delete_raw":
        return StorageAction.DELETE
    if name == "list_raw":
        return StorageAction.LIST
    if name in {
        "find_by_entity_text_hash",
        "find_by_linked_memory_id",
    }:
        return StorageAction.SEARCH
    if name in {"search", "recall", "seed_ids"}:
        return StorageAction.SEARCH
    if name in {"get", "mget", "exists", "scan", "list", "stat"}:
        return StorageAction.GET
    if name in {"scopes", "usage", "purge"}:
        return StorageAction.ADMIN
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
        raw: RawDataStore | Any | None = None,
        raw_store: RawDataStore | Any | None = None,
        entity: EntityStore | Any | None = None,
        kv_ports: dict[str, KVStore] | None = None,
        vector_ports: dict[str, VectorStore] | None = None,
        fulltext_ports: dict[str, FulltextStore] | None = None,
        graph_ports: dict[str, GraphStore] | None = None,
        fusion_ports: dict[str, FusionStore] | None = None,
        fs_ports: dict[str, FSStore] | None = None,
        raw_ports: dict[str, RawDataStore | Any] | None = None,
        entity_ports: dict[str, EntityStore | Any] | None = None,
        recallers: list[Any] | None = None,
        preferred_pipeline: RetrievalPipeline = RetrievalPipeline.RECALL_GET_RANK,
        security: StorageSecurity | None = None,
    ) -> None:
        selected_raw = raw if raw is not None else raw_store
        # 默认仍与正排共用同一个物理 KV，但只通过 RawDataStore 适配器暴露给上层。
        if selected_raw is None and kv is not None:
            selected_raw = KVRawDataStore(kv)
        selected_entity = adapt_entity_store(entity) if entity is not None else None
        self._stores = {
            StorageCapability.KV: kv,
            StorageCapability.VECTOR: vector,
            StorageCapability.FULLTEXT: fulltext,
            StorageCapability.GRAPH: graph,
            StorageCapability.FUSION: fusion,
            StorageCapability.FS: fs,
            StorageCapability.ENTITY: selected_entity,
        }
        self._capabilities = frozenset(
            capability
            for capability, store in self._stores.items()
            if store is not None
        )
        configured_ports = {
            StorageCapability.KV: kv_ports,
            StorageCapability.VECTOR: vector_ports,
            StorageCapability.FULLTEXT: fulltext_ports,
            StorageCapability.GRAPH: graph_ports,
            StorageCapability.FUSION: fusion_ports,
            StorageCapability.FS: fs_ports,
            StorageCapability.ENTITY: entity_ports,
        }
        self._named_stores: dict[StorageCapability, dict[str, Any]] = {}
        for capability, store in self._stores.items():
            ports = dict(configured_ports.get(capability) or {})
            if capability is StorageCapability.ENTITY:
                ports = {
                    name: adapt_entity_store(port)
                    for name, port in ports.items()
                }
            if store is not None:
                if capability is StorageCapability.ENTITY:
                    store = adapt_entity_store(store)
                    self._stores[capability] = store
                ports["default"] = store
            self._named_stores[capability] = ports
        # A named-only Entity port still provides the Entity capability.  Keep
        # capability discovery aligned with the actual port set even when no
        # ``default`` Entity backend is configured.
        has_named_entity_port = bool(self._named_stores[StorageCapability.ENTITY])
        if has_named_entity_port and StorageCapability.ENTITY not in self._capabilities:
            self._capabilities = self._capabilities | frozenset({StorageCapability.ENTITY})
        self._raw_stores = {
            name: adapt_raw_data_store(port)
            for name, port in (raw_ports or {}).items()
        }
        if selected_raw is not None:
            self._raw_stores["default"] = adapt_raw_data_store(selected_raw)
        self._recallers: list[Any] = list(recallers or [])
        self._preferred_pipeline = preferred_pipeline
        self._security = security or AllowAllStorageSecurity()
        self._proxies = {
            capability: {
                name: _AuthorizedStoreProxy(store, self._security, capability.value)
                for name, store in ports.items()
            }
            for capability, ports in self._named_stores.items()
        }
        self._raw_proxies = {
            name: _AuthorizedStoreProxy(store, self._security, "raw")
            for name, store in self._raw_stores.items()
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

    @property
    def raw(self) -> RawDataStore:
        return self.raw_port()

    @property
    def entity(self) -> EntityStore:
        return cast(EntityStore, self._port(StorageCapability.ENTITY))

    @staticmethod
    def _validate_units(scope: Scope, units: list[MemoryUnit]) -> None:
        invalid = [unit.id for unit in units if unit.scope != scope]
        if invalid:
            raise ValidationError(f"MemoryUnit scope differs from explicit scope: {invalid}")

    @property
    def recallers(self) -> list[Any]:
        """已接入的 recaller 列表（只读视图；外部不应原地修改）。"""
        return self._recallers

    def bind_recallers(self, recallers: list[Any]) -> None:
        """手动绑定检索适配器（测试/手工装配用）；同一 Storage 不允许绑定两套不同实例。"""
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

    def has_raw_port(self, name: str = "default") -> bool:
        return name in self._raw_stores

    def raw_shares_kv(self, name: str = "default") -> bool:
        """Report physical backend sharing for raw-data accounting.

        Raw ports are authorized proxies at the public boundary, so inspect
        the underlying adapter only for this non-data identity check.
        """
        if name != "default":
            return False
        main_kv = self._stores[StorageCapability.KV]
        if main_kv is None:
            return False
        raw_store = self._raw_stores.get(name)
        if raw_store is None:
            # A custom Storage may omit the raw port and rely on the base
            # contract's implicit KV-backed fallback. Composite normally
            # materializes this adapter in __init__.
            return True
        checker = getattr(raw_store, "shares_backend_with", None)
        return bool(checker(main_kv)) if callable(checker) else False

    def has_entity_port(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.ENTITY, name)

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

    def raw_port(self, name: str = "default") -> RawDataStore:
        try:
            return cast(RawDataStore, self._raw_proxies[name])
        except KeyError as exc:
            raise UnsupportedStorageCapabilityError(
                f"storage capability is not available: raw.{name}"
            ) from exc

    def entity_port(self, name: str = "default") -> EntityStore:
        return cast(EntityStore, self._port(StorageCapability.ENTITY, name))

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._preferred_pipeline

    def scopes(self) -> list[Scope]:
        found: dict[tuple[str, str, str, str, str], Scope] = {}
        for scope in self._raw_kv().scopes():
            found[_scope_key(scope)] = scope
        for raw_proxy in self._raw_proxies.values():
            for scope in raw_proxy.scopes():
                found.setdefault(_scope_key(scope), scope)
        return list(found.values())

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
        for store in self._raw_stores.values():
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


def _optional_store(
    producer: type[Factory], config: Any, field: str, *, include_default: bool = False
) -> Any | None:
    if field not in config.params:
        return producer.build("memory", {}, config.ctx) if include_default else None
    return producer.dep(config, field)


def _optional_raw_store(config: Any) -> RawDataStore | None:
    """解析可选的原文 KV 后端，未配置时由 Composite 复用真源 KV。"""
    if "raw_store" not in config.params:
        return None
    return KVRawDataStore(KvProducer.dep(config, "raw_store"))


def _named_ports(producer: type[Factory], config: Any) -> dict[str, Any]:
    namespace = config.ctx.namespaces.get(producer.TOP_NAME, {})
    return {
        name: producer.build_named(name, config.ctx)
        for name in ("layers_l0", "layers_l1")
        if name in namespace
    }


def _named_entity_ports(config: Any) -> dict[str, Any]:
    namespace = config.ctx.namespaces.get(EntityStoreProducer.TOP_NAME, {})
    return {
        name: EntityStoreProducer.build_named(name, config.ctx)
        for name in namespace
        if name != "default"
    }


def _assemble_recallers(config: Any, *, storage: "CompositeStorage") -> list[Any]:
    """按能力开关组装召回路；每路 recaller 自取其 Store，可被 config 各自覆盖。

    构建期同步执行，装配错误 fail-fast。具名构建（``config.name`` 非空）由 ``_build``
    预注册进具名缓存，``RecallerProducer.dep`` 走具名引用路径，recaller builder 内
    ``StorageProducer.resolve`` 命中缓存打破循环。匿名构建无缓存键，此处用合成名
    （``id(storage)`` 保证唯一）预注册本实例，改走 ``RecallerProducer.build`` 直接把
    storage 引用注入 params，让 builder 内的 ``resolve`` 走第一分支（``cls.dep``）
    命中合成名缓存——避免落到第三分支再建一个匿名 CompositeStorage 触发递归。
    """
    from jiuwen_memory.retrieval.recaller import RecallerProducer

    if config.name:
        # 具名构建：recaller 命名空间下声明的具名实例带 ``storage: <name>`` 引用，
        # ``dep`` 走 ``build_named`` 命中缓存即可，无需注入。
        def _dep(key: str, default_target: str) -> Any:
            return RecallerProducer.dep(config, key, default=default_target)
    else:
        # 匿名构建：无 recaller 命名空间，用合成名注册 + 直接 build 注入 storage 引用，
        # 让 builder 内 ``StorageProducer.resolve`` 走 ``cls.dep`` 第一分支命中缓存。
        synthetic_name = f"__anon_storage_{id(storage)}__"
        StorageProducer.put(synthetic_name, storage)

        def _dep(key: str, default_target: str) -> Any:
            target = config.get(key, default_target)
            return RecallerProducer.build(target, {"storage": synthetic_name}, config.ctx)

    recallers = [_dep("keyword_recaller", "keyword")]
    if config.get("vector_enabled", True):
        recallers.append(_dep("vector_recaller", "vector"))
    if config.get("graph_enabled", True):
        recallers.append(_dep("graph_recaller", "graph"))
    # L0/L1 分层召回：layers_index_enabled 默认 true（与构建侧对齐：默认建默认查）。
    # recaller 内部 store 为 None 时 recall 返空，不破坏其他路（向后兼容）。
    if config.get("layers_index_enabled", True):
        recallers.append(_dep("keyword_l0_recaller", "keyword_l0"))
        recallers.append(_dep("keyword_l1_recaller", "keyword_l1"))
        if config.get("vector_enabled", True):
            recallers.append(_dep("vector_l0_recaller", "vector_l0"))
            recallers.append(_dep("vector_l1_recaller", "vector_l1"))
    return recallers


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
    storage = CompositeStorage(
        kv=KvProducer.dep(config, default="memory"),
        raw=_optional_raw_store(config),
        vector=_optional_store(
            VectorProducer,
            config,
            "vector_store",
            include_default=config.get("__default_capabilities", False),
        ),
        fulltext=_optional_store(
            FulltextProducer,
            config,
            "fulltext_store",
            include_default=config.get("__default_capabilities", False),
        ),
        graph=_optional_store(
            GraphProducer,
            config,
            "graph_store",
            include_default=config.get("__default_capabilities", False),
        ),
        fusion=_optional_store(FusionProducer, config, "fusion_store"),
        fs=_optional_store(FsProducer, config, "fs_store"),
        entity=_optional_store(EntityStoreProducer, config, "entity_store"),
        vector_ports=_named_ports(VectorProducer, config),
        fulltext_ports=_named_ports(FulltextProducer, config),
        entity_ports=_named_entity_ports(config),
        preferred_pipeline=preferred_pipeline,
    )
    # 具名构建先把实例预注册进缓存再组装召回路，打破循环依赖（recaller builder
    # 经 ``StorageProducer.resolve`` 回取时命中缓存）；匿名构建无缓存键，由
    # ``_assemble_recallers`` 内部用合成名注册并注入 storage 引用。装配错误构建期暴露。
    if config.name:
        StorageProducer.put(config.name, storage)
    storage.bind_recallers(_assemble_recallers(config, storage=storage))
    return storage
