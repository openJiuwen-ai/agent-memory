"""上层屏蔽底层装配的统一 Storage 契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from common.type_def import (
    CandidateFuser,
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    RankedStorageResult,
    RecallChannel,
    RecallResult,
    RetrievalPipeline,
    Scope,
    ScoredMemoryUnit,
    ScoredUnit,
)

from .fs import FSStore
from .fulltext import FulltextStore
from .fusion import FusionStore
from .graph import GraphStore
from .kv import KVStore
from .security import StorageAccessContext, StorageSecurity
from .types import MemoryListResult
from .vector import VectorStore


class StorageCapability(str, Enum):
    KV = "kv"
    VECTOR = "vector"
    FULLTEXT = "fulltext"
    GRAPH = "graph"
    FUSION = "fusion"
    FS = "fs"


class Storage(ABC):
    @property
    @abstractmethod
    def security(self) -> StorageSecurity:
        ...

    @abstractmethod
    def capabilities(self) -> frozenset[StorageCapability]:
        ...

    def has_kv(self) -> bool:
        return StorageCapability.KV in self.capabilities()

    def has_vector(self) -> bool:
        return StorageCapability.VECTOR in self.capabilities()

    def has_fulltext(self) -> bool:
        return StorageCapability.FULLTEXT in self.capabilities()

    def has_graph(self) -> bool:
        return StorageCapability.GRAPH in self.capabilities()

    def has_fusion(self) -> bool:
        return StorageCapability.FUSION in self.capabilities()

    def has_fs(self) -> bool:
        return StorageCapability.FS in self.capabilities()

    @property
    @abstractmethod
    def kv(self) -> KVStore:
        ...

    @property
    @abstractmethod
    def vector(self) -> VectorStore:
        ...

    @property
    @abstractmethod
    def fulltext(self) -> FulltextStore:
        ...

    @property
    @abstractmethod
    def graph(self) -> GraphStore:
        ...

    @property
    @abstractmethod
    def fusion(self) -> FusionStore:
        ...

    @property
    @abstractmethod
    def fs(self) -> FSStore:
        ...

    @abstractmethod
    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        ...

    @abstractmethod
    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        ...

    @abstractmethod
    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        ...

    @abstractmethod
    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> None:
        ...

    @abstractmethod
    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredUnit]:
        ...

    @abstractmethod
    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredMemoryUnit]:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    def health(self) -> None:
        ...
