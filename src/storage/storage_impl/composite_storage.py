"""由现有 Store 和 Recaller 组合而成的默认统一 Storage。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, cast

from common.errors import (
    NotFoundError,
    StorageRetrievalError,
    UnsupportedStorageCapabilityError,
    ValidationError,
    safe_error_message,
)
from common.type_def import (
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
from common.type_def.memory_codec import dumps, loads
from storage.fs import FSStore
from storage.fulltext import FulltextStore
from storage.fusion import FusionStore
from storage.graph import GraphStore
from storage.kv import KVStore
from storage.security import (
    AllowAllStorageSecurity,
    StorageAccessContext,
    StorageAction,
    StorageSecurity,
)
from storage.storage import Storage, StorageCapability
from storage.types import MemoryListResult
from storage.vector import VectorStore


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
        self._recallers = list(recallers or [])
        self._preferred_pipeline = preferred_pipeline
        self._security = security or AllowAllStorageSecurity()
        self._proxies = {
            capability: _AuthorizedStoreProxy(store, self._security, capability.value)
            for capability, store in self._stores.items()
            if store is not None
        }

    @property
    def security(self) -> StorageSecurity:
        return self._security

    def capabilities(self) -> frozenset[StorageCapability]:
        return self._capabilities

    def _port(self, capability: StorageCapability) -> Any:
        try:
            return self._proxies[capability]
        except KeyError as exc:
            raise UnsupportedStorageCapabilityError(
                f"storage capability is not available: {capability.value}"
            ) from exc

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

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._preferred_pipeline

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

    @staticmethod
    def _validate_units(scope: Scope, units: list[MemoryUnit]) -> None:
        invalid = [unit.id for unit in units if unit.scope != scope]
        if invalid:
            raise ValidationError(f"MemoryUnit scope differs from explicit scope: {invalid}")

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.ADD, "memory_unit")
        self._validate_units(scope, units)
        kv = self._raw_kv()
        for unit in units:
            kv.insert(scope, memory_key(unit.id), dumps(unit))

    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.UPDATE, "memory_unit")
        self._validate_units(scope, units)
        kv = self._raw_kv()
        for unit in units:
            kv.update(scope, memory_key(unit.id), dumps(unit))

    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._authorize(access, scope, StorageAction.DELETE, "memory_unit")
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

    def _get_units(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        kv = self._raw_kv()
        units: list[MemoryUnit] = []
        for unit_id in unit_ids:
            try:
                unit = loads(kv.get(scope, memory_key(unit_id)))
            except NotFoundError:
                continue
            if unit is not None:
                units.append(unit)
        return units

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
        for capability in self._capabilities:
            store = self._stores[capability]
            if store is not None:
                store.security.health()
                store.health()


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
