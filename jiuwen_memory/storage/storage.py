"""上层屏蔽底层装配的统一 Storage 契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
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
from .types import IndexRemoveMode, IndexWriteMode, MemoryListResult
from .vector import VectorStore


class StorageProducer(Factory):
    """统一 Storage 的注册式工厂。"""

    TOP_NAME = "storage"

    @classmethod
    def resolve(cls, config, *, default_target: str = "composite") -> Storage:
        """解析组件依赖，缺省时优先复用 ``storage.default`` 具名实例。"""
        if cls.TOP_NAME in config.params:
            storage = cls.dep(config)
        elif "default" in config.ctx.namespaces.get(cls.TOP_NAME, {}):
            storage = cls.build_named("default", config.ctx)
        else:
            store_params = {
                name: config.params[name]
                for name in ("kv_store", "vector_store", "fulltext_store", "graph_store")
                if name in config.params
            }
            store_params["__default_capabilities"] = True
            storage = cls.build(
                default_target,
                store_params,
                config.ctx,
            )
        if not isinstance(storage, Storage):
            raise TypeError(f"StorageProducer assembled {type(storage).__name__}, expected Storage")
        return storage


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

    def has_kv_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_kv()

    def has_vector_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_vector()

    def has_fulltext_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_fulltext()

    def has_graph_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_graph()

    def has_fusion_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_fusion()

    def has_fs_port(self, name: str = "default") -> bool:
        return name == "default" and self.has_fs()

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

    def kv_port(self, name: str = "default") -> KVStore:
        if name == "default":
            return self.kv
        raise UnsupportedStorageCapabilityError(f"storage capability is not available: kv.{name}")

    def vector_port(self, name: str = "default") -> VectorStore:
        if name == "default":
            return self.vector
        raise UnsupportedStorageCapabilityError(
            f"storage capability is not available: vector.{name}"
        )

    def fulltext_port(self, name: str = "default") -> FulltextStore:
        if name == "default":
            return self.fulltext
        raise UnsupportedStorageCapabilityError(
            f"storage capability is not available: fulltext.{name}"
        )

    def graph_port(self, name: str = "default") -> GraphStore:
        if name == "default":
            return self.graph
        raise UnsupportedStorageCapabilityError(
            f"storage capability is not available: graph.{name}"
        )

    def fusion_port(self, name: str = "default") -> FusionStore:
        if name == "default":
            return self.fusion
        raise UnsupportedStorageCapabilityError(
            f"storage capability is not available: fusion.{name}"
        )

    def fs_port(self, name: str = "default") -> FSStore:
        if name == "default":
            return self.fs
        raise UnsupportedStorageCapabilityError(f"storage capability is not available: fs.{name}")

    @abstractmethod
    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        ...

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """返回当前统一存储内已有记忆数据的作用域。"""
        ...

    @abstractmethod
    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        """写入一批记忆。

        覆盖范围是**实现相关**的：该实现按其能力落地——``CompositeStorage`` 无投影能力，
        只落记忆本体；一体化后端可一次建立正排、倒排、向量、图。

        ``mode=RETRIEVAL_ONLY`` 表示记忆本体已存在、只需补建检索索引；不具备检索索引
        能力的实现此时为空操作。
        """
        ...

    @abstractmethod
    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        """更新一批记忆。覆盖范围同 :meth:`add`。

        ``mode=FORWARD_ONLY`` 表示只回写记忆本体、检索索引不动。无法拆分两者的实现应
        至少保证本体被写到——多刷新一次检索索引无害，漏写本体则丢数据。
        """
        ...

    @abstractmethod
    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
        access: StorageAccessContext | None = None,
    ) -> None:
        """删除一批记忆。覆盖范围同 :meth:`add`。

        ``mode=SOFT`` 为软删除：只移出检索索引（search/recall 不再召回），记忆本体
        保留，`get`/`list` 仍可读——用于归档/遗忘等非破坏式治理；不具备检索索引能力
        的实现此时为空操作。``mode=HARD`` 为硬删除：检索索引与记忆本体一并物理删除。
        """
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
