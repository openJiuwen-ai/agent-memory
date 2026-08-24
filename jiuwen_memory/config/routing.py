# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ActiveRouter / Routing* — 按 ConfigSource 的 ``*.active`` 在已预装实例间切换。

**次选路径**（S08）：同实现多套 model/key/url 应优先走调用路径晚绑定
（:mod:`config.binding`），不要为此拆多套同构具名实例。

本模块用于异质实现互切（如 hashing ↔ openai、memory ↔ redis）或产品明确要求的
实例隔离：装配期注入多套实现；运行期只改 ``*.active``，不经业务 API。

分层：
- **Store 级**（F01）：``RoutingKVStore`` / ``RoutingVectorStore`` 等 + ``*_store.active``
- **Storage 级**（F02）：``RoutingStorage`` + ``storage.active``（已预装完整 ``Storage`` 实例动态配置）

均为方案 A：不注册 YAML ``target: routing``；产品手工注入。默认拓扑不预装多后端。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, BinaryIO, Generic, TypeVar

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import (
    CandidateFuser,
    ChatMessage,
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
from jiuwen_memory.config.active import resolve_active_name
from jiuwen_memory.config.config_source import ConfigSource
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.fs import FSStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.fusion import FusionStore
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageSecurity
from jiuwen_memory.storage.storage import Storage, StorageCapability
from jiuwen_memory.storage.types import (
    Document,
    Edge,
    FileStat,
    FusionQuery,
    FusionRecord,
    GraphQuery,
    IndexRemoveMode,
    IndexWriteMode,
    KVMemoryListResult,
    MemoryListResult,
    Node,
    ScoredHit,
    ScoredID,
    TextQuery,
    VectorQuery,
    VectorRecord,
)
from jiuwen_memory.storage.vector import VectorStore

T = TypeVar("T")


class ActiveRouter(Generic[T]):
    """在已预装 ``instances`` 中按 ``<namespace>.active`` 解析当前实例。

    Args:
        namespace: 如 ``embedder`` / ``llm`` / ``reranker``
        instances: 装配期已创建的具名实例表（注册 ≠ 预装配）
        config_source: 读取 ``<namespace>.active`` 的来源
        default_name: ConfigSource 未设置 active 时的默认实例名（必须已在 instances 中）
    """

    def __init__(
        self,
        *,
        namespace: str,
        instances: dict[str, T],
        config_source: ConfigSource,
        default_name: str,
    ) -> None:
        if not instances:
            raise ValueError(f"{namespace} ActiveRouter 需要至少一个预装实例")
        if default_name not in instances:
            raise ValueError(
                f"{namespace} default_name {default_name!r} 不在 instances 中："
                f"{sorted(instances)}"
            )
        self._namespace = namespace
        self._instances = dict(instances)
        self._config_source = config_source
        self._default_name = default_name

    @property
    def active_name(self) -> str:
        """当前解析出的具名实例名（与 :meth:`get` 同一套规则）。"""
        return resolve_active_name(
            self._config_source,
            namespace=self._namespace,
            available=tuple(self._instances),
            default=self._default_name,
        )

    def get(self) -> T:
        """返回当前 active 对应实例；未知 active 名抛 :class:`ValidationError`。"""
        name = resolve_active_name(
            self._config_source,
            namespace=self._namespace,
            available=tuple(self._instances),
            default=self._default_name,
        )
        return self._instances[name]


class RoutingEmbedder(Embedder):
    """Embedder 门面：每次 ``embed`` / ``dimension`` / ``health`` 委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[Embedder]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``EMBEDDER``。"""
        return PluginType.EMBEDDER

    def health(self) -> None:
        """委托当前 active Embedder 探活。"""
        self._router.get().health()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """委托当前 active Embedder 向量化。"""
        return self._router.get().embed(texts)

    def dimension(self) -> int:
        """委托当前 active Embedder 的维度（切换实例后可能变化，调用方需自洽）。"""
        return self._router.get().dimension()


class RoutingLLM(LLM):
    """LLM 门面：每次 ``chat`` / ``health`` 委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[LLM]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``LLM``。"""
        return PluginType.LLM

    def health(self) -> None:
        """委托当前 active LLM 探活。"""
        self._router.get().health()

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        """委托当前 active LLM 对话。"""
        return self._router.get().chat(messages, **options)


class RoutingReranker(Reranker):
    """Reranker 门面：每次打分 / 探活委托当前 active 实例。"""

    def __init__(self, router: ActiveRouter[Reranker]) -> None:
        self._router = router

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``RERANKER``。"""
        return PluginType.RERANKER

    def health(self) -> None:
        """委托当前 active Reranker 探活。"""
        self._router.get().health()

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """委托当前 active Reranker 打分。"""
        return self._router.get().rerank(query, texts)


class RoutingKVStore(KVStore):
    """KVStore 门面：每次 CRUD / list / scan 委托当前 ``kv_store.active`` 实例。"""

    def __init__(self, router: ActiveRouter[KVStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._router.get().insert(scope, key, value, ttl)

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._router.get().update(scope, key, value, ttl)

    def delete(self, scope: Scope, key: str) -> None:
        self._router.get().delete(scope, key)

    def get(self, scope: Scope, key: str) -> bytes:
        return self._router.get().get(scope, key)

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        return self._router.get().mget(scope, keys)

    def exists(self, scope: Scope, key: str) -> bool:
        return self._router.get().exists(scope, key)

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        return self._router.get().scan(scope, prefix)

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        return self._router.get().list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        return self._router.get().scopes()


class RoutingVectorStore(VectorStore):
    """VectorStore 门面：每次 CRUD / search / recall 委托当前 ``vector_store.active``。"""

    def __init__(self, router: ActiveRouter[VectorStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def insert(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._router.get().insert(scope, records)

    def update(self, scope: Scope, records: list[VectorRecord]) -> None:
        self._router.get().update(scope, records)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        self._router.get().delete(scope, ids)

    def get(self, scope: Scope, ids: list[str]) -> list[VectorRecord]:
        return self._router.get().get(scope, ids)

    def search(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        return self._router.get().search(scope, query)

    def recall(
        self,
        scope: Scope,
        query: VectorQuery,
        output_fields: list[str] | None = None,
    ) -> list[ScoredHit]:
        return self._router.get().recall(scope, query, output_fields=output_fields)

    def score_higher_is_better(self) -> bool:
        return self._router.get().score_higher_is_better()


class RoutingFulltextStore(FulltextStore):
    """FulltextStore 门面：委托当前 ``fulltext_store.active`` 实例。"""

    def __init__(self, router: ActiveRouter[FulltextStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def insert(self, scope: Scope, docs: list[Document]) -> None:
        self._router.get().insert(scope, docs)

    def update(self, scope: Scope, docs: list[Document]) -> None:
        self._router.get().update(scope, docs)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        self._router.get().delete(scope, ids)

    def get(self, scope: Scope, ids: list[str]) -> list[Document]:
        return self._router.get().get(scope, ids)

    def search(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        return self._router.get().search(scope, query)


class RoutingGraphStore(GraphStore):
    """GraphStore 门面：委托当前 ``graph_store.active`` 实例。"""

    def __init__(self, router: ActiveRouter[GraphStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def seed_ids(self, scope: Scope, tokens: set[str]) -> list[str]:
        return self._router.get().seed_ids(scope, tokens)

    def insert(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        self._router.get().insert(scope, nodes=nodes, edges=edges)

    def update(
        self,
        scope: Scope,
        nodes: list[Node] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        self._router.get().update(scope, nodes=nodes, edges=edges)

    def delete(
        self,
        scope: Scope,
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> None:
        self._router.get().delete(scope, node_ids=node_ids, edge_ids=edge_ids)

    def get(self, scope: Scope, node_ids: list[str]) -> list[Node]:
        return self._router.get().get(scope, node_ids)

    def search(self, scope: Scope, query: GraphQuery) -> list[Node]:
        return self._router.get().search(scope, query)


class RoutingFusionStore(FusionStore):
    """FusionStore 门面：委托当前 ``fusion_store.active`` 实例。"""

    def __init__(self, router: ActiveRouter[FusionStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def insert(self, scope: Scope, records: list[FusionRecord]) -> None:
        self._router.get().insert(scope, records)

    def update(self, scope: Scope, records: list[FusionRecord]) -> None:
        self._router.get().update(scope, records)

    def delete(self, scope: Scope, ids: list[str]) -> None:
        self._router.get().delete(scope, ids)

    def get(self, scope: Scope, ids: list[str]) -> list[FusionRecord]:
        return self._router.get().get(scope, ids)

    def search(self, scope: Scope, query: FusionQuery) -> list[ScoredID]:
        return self._router.get().search(scope, query)


class RoutingFSStore(FSStore):
    """FSStore 门面：委托当前 ``fs_store.active`` 实例。"""

    def __init__(self, router: ActiveRouter[FSStore]) -> None:
        self._router = router

    def store_type(self) -> StoreType:
        return self._router.get().store_type()

    def health(self) -> None:
        self._router.get().health()

    def insert(self, scope: Scope, key: str, data: BinaryIO) -> str:
        return self._router.get().insert(scope, key, data)

    def update(self, scope: Scope, ref: str, data: BinaryIO) -> str:
        return self._router.get().update(scope, ref, data)

    def delete(self, scope: Scope, ref: str) -> None:
        self._router.get().delete(scope, ref)

    def get(self, scope: Scope, ref: str) -> BinaryIO:
        return self._router.get().get(scope, ref)

    def stat(self, scope: Scope, ref: str) -> FileStat:
        return self._router.get().stat(scope, ref)


class _LazyStorePort:
    """惰性 Store 端口：属性访问时再 ``resolve()``，使构造期缓存的引用仍随 active 切换。

    IndexBuilder / Recaller 常在 ``__init__`` 里 ``self._vector = storage.vector``。
    若 ``storage.vector`` 直接返回某一实例的裸 Store，``storage.active`` 切换后仍打到旧实例。
    本代理每次方法查找都重新解析当前实例端口（F02 决策 4 选项 b）。
    """

    __slots__ = ("_resolve",)

    def __init__(self, resolve: Callable[[], Any]) -> None:
        object.__setattr__(self, "_resolve", resolve)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        try:
            current = self._resolve()
        except Exception as exc:
            # repr 不得向外抛；解析失败时仍给出可读占位
            return f"<LazyStorePort unresolved: {type(exc).__name__}: {exc}>"
        return f"<LazyStorePort -> {type(current).__name__}>"


class RoutingStorage(Storage):
    """按 ``storage.active`` 在已预装的完整 ``Storage`` 实例间动态选用（F02）。

    与 F01 ``Routing*Store`` 分层：本类切换的是整颗 ``Storage``（Composite / 一体化实现），
    各实例内部仍可继续用 Store 级 Routing 或同实现 url 晚绑定。

    领域方法每次 ``router.get()`` 后委托；``.kv`` / ``.vector`` / ``*_port`` 返回稳定的
    :class:`_LazyStorePort`，兼容构造期缓存端口引用的 IndexBuilder/Recaller。

    方案 A：不注册 YAML ``storage.target: routing``；产品手工 ``put`` / 自注册 producer。
    EncryptedKV 只包在各预装实例内部，不在本类外包一层。
    """

    def __init__(self, router: ActiveRouter[Storage]) -> None:
        self._router = router
        # 端口代理只建一次：对象身份稳定，内部每次调用再 get() 当前实例
        self._lazy_kv = _LazyStorePort(lambda: self._router.get().kv)
        self._lazy_vector = _LazyStorePort(lambda: self._router.get().vector)
        self._lazy_fulltext = _LazyStorePort(lambda: self._router.get().fulltext)
        self._lazy_graph = _LazyStorePort(lambda: self._router.get().graph)
        self._lazy_fusion = _LazyStorePort(lambda: self._router.get().fusion)
        self._lazy_fs = _LazyStorePort(lambda: self._router.get().fs)

    @property
    def security(self) -> StorageSecurity:
        return self._active().security

    @property
    def kv(self) -> KVStore:
        return self._lazy_kv  # type: ignore[return-value]

    @property
    def vector(self) -> VectorStore:
        return self._lazy_vector  # type: ignore[return-value]

    @property
    def fulltext(self) -> FulltextStore:
        return self._lazy_fulltext  # type: ignore[return-value]

    @property
    def graph(self) -> GraphStore:
        return self._lazy_graph  # type: ignore[return-value]

    @property
    def fusion(self) -> FusionStore:
        return self._lazy_fusion  # type: ignore[return-value]

    @property
    def fs(self) -> FSStore:
        return self._lazy_fs  # type: ignore[return-value]

    def capabilities(self) -> frozenset[StorageCapability]:
        return self._active().capabilities()

    def has_kv_port(self, name: str = "default") -> bool:
        return self._active().has_kv_port(name)

    def has_vector_port(self, name: str = "default") -> bool:
        return self._active().has_vector_port(name)

    def has_fulltext_port(self, name: str = "default") -> bool:
        return self._active().has_fulltext_port(name)

    def has_graph_port(self, name: str = "default") -> bool:
        return self._active().has_graph_port(name)

    def has_fusion_port(self, name: str = "default") -> bool:
        return self._active().has_fusion_port(name)

    def has_fs_port(self, name: str = "default") -> bool:
        return self._active().has_fs_port(name)

    def kv_port(self, name: str = "default") -> KVStore:
        return _LazyStorePort(lambda n=name: self._router.get().kv_port(n))  # type: ignore[return-value]

    def vector_port(self, name: str = "default") -> VectorStore:
        return _LazyStorePort(lambda n=name: self._router.get().vector_port(n))  # type: ignore[return-value]

    def fulltext_port(self, name: str = "default") -> FulltextStore:
        return _LazyStorePort(lambda n=name: self._router.get().fulltext_port(n))  # type: ignore[return-value]

    def graph_port(self, name: str = "default") -> GraphStore:
        return _LazyStorePort(lambda n=name: self._router.get().graph_port(n))  # type: ignore[return-value]

    def fusion_port(self, name: str = "default") -> FusionStore:
        return _LazyStorePort(lambda n=name: self._router.get().fusion_port(n))  # type: ignore[return-value]

    def fs_port(self, name: str = "default") -> FSStore:
        return _LazyStorePort(lambda n=name: self._router.get().fs_port(n))  # type: ignore[return-value]

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._active().preferred_retrieval_pipeline()

    def scopes(self) -> list[Scope]:
        return self._active().scopes()

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        # 意图参数原样下传：落地范围由当前 active 实例按自身能力决定，本类只做路由。
        self._active().add(scope, units, mode=mode, access=access)

    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._active().update(scope, units, mode=mode, access=access)

    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._active().delete(scope, unit_ids, mode=mode, access=access)

    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        return self._active().get(scope, unit_ids, access=access)

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
        return self._active().list(
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
            access=access,
        )

    def recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredUnit]:
        return self._active().recall(
            scope,
            query,
            channels=channels,
            recall_limit=recall_limit,
            access=access,
        )

    def recall_and_get(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        channels: list[RecallChannel] | None,
        recall_limit: int,
        access: StorageAccessContext | None = None,
    ) -> RecallResult[ScoredMemoryUnit]:
        return self._active().recall_and_get(
            scope,
            query,
            channels=channels,
            recall_limit=recall_limit,
            access=access,
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
        return self._active().retrieve(
            scope,
            query,
            fuser,
            channels=channels,
            recall_limit=recall_limit,
            rank_limit=rank_limit,
            access=access,
        )

    def health(self) -> None:
        self._active().health()

    def _active(self) -> Storage:
        return self._router.get()

