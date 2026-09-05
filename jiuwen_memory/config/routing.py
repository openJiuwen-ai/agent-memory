# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ActiveRouter / Routing* — 按 ConfigSource 的 ``*.active`` 在已预装实例间切换。

**次选路径**（S08）：同实现多套 model/key/url 应优先走调用路径晚绑定
（:mod:`config.binding`），不要为此拆多套同构具名实例。

本模块用于异质实现互切（如 hashing ↔ openai、memory ↔ redis）或产品明确要求的
实例隔离：装配期注入多套实现；运行期只改 ``*.active``，不经业务 API。

分层：
- **Store 级**（F01）：``RoutingKVStore`` / ``RoutingVectorStore`` 等 + ``*_store.active``
- **StoreManager 级**（F02/F08）：``RoutingStoreManager`` + ``RoutingDomainStore``
  + ``store_manager.active``（已预装完整 ``StoreManager`` 实例动态配置）

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
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.fs import FSStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.fusion import FusionStore
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageSecurity
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManager
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
    若 ``storage.vector`` 直接返回某一实例的裸 Store，``store_manager.active`` 切换后仍打到旧实例。
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


class RoutingStoreManager(StoreManager):
    """按 ``store_manager.active`` 在已预装的完整 ``StoreManager`` 实例间动态选用（F02）。

    与 F01 ``Routing*Store`` 分层：本类切换的是整颗 ``StoreManager``（Composite / 一体化
    实现），各实例内部仍可继续用 Store 级 Routing 或同实现 url 晚绑定。

    管理面方法每次 ``router.get()`` 后委托；端口方法（``kv(name)`` / ``vector(name)``
    等）返回稳定的 :class:`_LazyStorePort`——代理按 ``(capability, name)`` 缓存，同键
    多次调用返回同一代理对象（身份稳定），内部每次方法调用再解析当前实例端口，兼容
    构造期缓存端口引用的 IndexBuilder/Recaller。:meth:`domain_store` 返回按 name
    惰性缓存的 :class:`RoutingDomainStore`（同名身份稳定），其内部每次方法调用再解析
    当前 active 实例的同名数据面，使数据面调用同样随 ``store_manager.active`` 切换。

    方案 A：不注册 YAML ``storage.target: routing``；产品手工 ``put`` / 自注册 producer。
    EncryptedKV 只包在各预装实例内部，不在本类外包一层。
    """

    def __init__(self, router: ActiveRouter[StoreManager]) -> None:
        self._router = router
        # 端口代理按 (capability, name) 缓存：同键身份稳定（F02「构造期缓存端口引用
        # 仍随 active 切换」依赖），内部每次调用再 get() 当前实例端口。
        self._lazy_ports: dict[tuple[str, str], _LazyStorePort] = {}
        # 命名数据面代理按 name 惰性缓存：同名身份稳定，内部每次方法调用再
        # get() + domain_store(name) 解析当前实例。
        self._domain_stores: dict[str, RoutingDomainStore] = {}

    @property
    def security(self) -> StorageSecurity:
        return self._active().security

    def capabilities(self) -> frozenset[StorageCapability]:
        return self._active().capabilities()

    def domain_store(self, name: str = "default") -> RoutingDomainStore:
        proxy = self._domain_stores.get(name)
        if proxy is None:
            proxy = RoutingDomainStore(self._router, name)
            self._domain_stores[name] = proxy
        return proxy

    def has_domain_store(self, name: str = "default") -> bool:
        return self._active().has_domain_store(name)

    def _lazy_port(self, capability: str, name: str) -> _LazyStorePort:
        key = (capability, name)
        port = self._lazy_ports.get(key)
        if port is None:
            port = _LazyStorePort(
                lambda c=capability, n=name: getattr(self._router.get(), c)(n)
            )
            self._lazy_ports[key] = port
        return port

    def kv(self, name: str = "default") -> KVStore:
        return self._lazy_port("kv", name)  # type: ignore[return-value]

    def vector(self, name: str = "default") -> VectorStore:
        return self._lazy_port("vector", name)  # type: ignore[return-value]

    def fulltext(self, name: str = "default") -> FulltextStore:
        return self._lazy_port("fulltext", name)  # type: ignore[return-value]

    def graph(self, name: str = "default") -> GraphStore:
        return self._lazy_port("graph", name)  # type: ignore[return-value]

    def fusion(self, name: str = "default") -> FusionStore:
        return self._lazy_port("fusion", name)  # type: ignore[return-value]

    def fs(self, name: str = "default") -> FSStore:
        return self._lazy_port("fs", name)  # type: ignore[return-value]

    def entity(self, name: str = "default") -> EntityStore:
        return self._lazy_port("entity", name)  # type: ignore[return-value]

    def has_kv(self, name: str = "default") -> bool:
        return self._active().has_kv(name)

    def has_vector(self, name: str = "default") -> bool:
        return self._active().has_vector(name)

    def has_fulltext(self, name: str = "default") -> bool:
        return self._active().has_fulltext(name)

    def has_graph(self, name: str = "default") -> bool:
        return self._active().has_graph(name)

    def has_fusion(self, name: str = "default") -> bool:
        return self._active().has_fusion(name)

    def has_fs(self, name: str = "default") -> bool:
        return self._active().has_fs(name)

    def has_entity(self, name: str = "default") -> bool:
        return self._active().has_entity(name)

    def health(self) -> None:
        self._active().health()

    def _active(self) -> StoreManager:
        return self._router.get()


class RoutingDomainStore(DomainStore):
    """按 ``store_manager.active`` 委托当前 active ``StoreManager`` 的同名 ``domain_store(name)``。

    共享 :class:`RoutingStoreManager` 的 router（指向底层 ``StoreManager``）；持有
    数据面名 ``name``，每次方法调用 ``router.get().domain_store(self._name).<method>``
    委托当前 active 实例的同名数据面。对象身份在 ``RoutingStoreManager`` 内稳定
    （按 name 惰性缓存一次），保证 :meth:`domain_store` 同名多次返回同一实例——但
    内部每次方法调用都重解析 active，使切换后调用打到新实例。

    **不实现 ``bind_recallers``**：active 切换语义要求各预装实例装配期各自绑定
    recallers，对外只读委托；手工接线始终作用于各预装的 ``CompositeDomainStore``
    实例，不作用于本类。
    """

    def __init__(self, router: ActiveRouter[StoreManager], name: str = "default") -> None:
        self._router = router
        self._name = name

    @property
    def security(self) -> StorageSecurity:
        return self._active_domain().security

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return self._active_domain().preferred_retrieval_pipeline()

    def scopes(self) -> list[Scope]:
        return self._active_domain().scopes()

    def add(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        # 意图参数原样下传：落地范围由当前 active 实例按自身能力决定，本类只做路由。
        self._active_domain().add(scope, units, mode=mode, access=access)

    def update(
        self,
        scope: Scope,
        units: list[MemoryUnit],
        *,
        mode: IndexWriteMode = IndexWriteMode.ALL,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._active_domain().update(scope, units, mode=mode, access=access)

    def delete(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        mode: IndexRemoveMode = IndexRemoveMode.HARD,
        access: StorageAccessContext | None = None,
    ) -> None:
        self._active_domain().delete(scope, unit_ids, mode=mode, access=access)

    def get(
        self,
        scope: Scope,
        unit_ids: list[str],
        *,
        access: StorageAccessContext | None = None,
    ) -> list[MemoryUnit]:
        return self._active_domain().get(scope, unit_ids, access=access)

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
        return self._active_domain().list(
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
        return self._active_domain().recall(
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
        return self._active_domain().recall_and_get(
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
        return self._active_domain().retrieve(
            scope,
            query,
            fuser,
            channels=channels,
            recall_limit=recall_limit,
            rank_limit=rank_limit,
            access=access,
        )

    def health(self) -> None:
        self._active_domain().health()

    def _active_domain(self) -> DomainStore:
        return self._router.get().domain_store(self._name)

