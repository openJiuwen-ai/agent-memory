# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CompositeStoreManager — 默认管理面实现。

组合七类 Store + 端口代理 + 健康聚合；数据面实例由 manager 工厂内的
``DomainStoreProducer.build`` 构建并经 :meth:`bind_domain_store` 注入（装配链路见
模块尾部 ``_build``）。召回路装配在 manager ``_build`` 末尾完成（F06 内收设计保留）：
按 ``vector_enabled`` / ``graph_enabled`` / ``layers_index_enabled`` 与 ``*_recaller``
配置同步组装，装配错误 fail-fast。

命名数据面（F07）：``domain_store(name)`` 多槽——``default`` 之外可经
``store_manager.<inst>.params.domain_stores`` 声明任意命名数据面（差异 = 检索
profile：``preferred_retrieval_pipeline`` + recaller 选择键覆盖）；各套共享同一
物理 Store 集。

命名端口（F07）：七类 ``*_store`` 命名空间下所有非 ``default`` 具名实例**全量自动**
成为端口（声明即端口）；encrypted 的明文 raw 若以具名声明会随之暴露，raw 推荐
inline 声明（见 F04/S06）。

recaller builder 会经 ``StoreManagerProducer.resolve`` 回取本 manager 实例，故工厂
先把构建中的实例预注册进具名缓存再组装召回路，打破循环依赖：具名构建用
``config.name`` 预注册；匿名构建用合成名（``id(manager)`` 唯一）预注册并把 manager
引用注入 recaller params。详见 F06/F07 特性文档。
"""

from __future__ import annotations

from typing import Any, cast

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def.entity import EntityOpType
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.storage.domain_store import DomainStore, DomainStoreProducer
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.entity_store import EntityStore, EntityStoreProducer
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.fulltext import FulltextProducer, FulltextStore
from jiuwen_memory.storage.fusion import FusionProducer, FusionStore
from jiuwen_memory.storage.graph import GraphProducer, GraphStore
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.security import (
    AllowAllStorageSecurity,
    StorageAction,
    StorageSecurity,
)
from jiuwen_memory.storage.store_manager import (
    StorageCapability,
    StoreManager,
    StoreManagerProducer,
)
from jiuwen_memory.storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)


class _AuthorizedStoreProxy:
    """给现有 Store 方法增加可选 access，同时避免暴露原始实例。"""

    def __init__(self, store: Any, security: StorageSecurity, resource: str) -> None:
        self._store = store
        self._security = security
        self._resource = resource

    def __getattr__(self, name: str) -> Any:
        from jiuwen_memory.common.type_def import Scope

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


def _action_for_store_method(name: str) -> Any:
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


# -- ENTITY 端口的授权适配（F07）------------------------------------------------ #
# EntityStore 是 BaseStore「scope 为显式第一入参」的唯一例外（首参 space_id: str），
# 且 execute_operations 的动作必须按 batch 内 op 类型派生——方法名不足以判定。故不复用
# 通用 _AuthorizedStoreProxy / _action_for_store_method，独立一套。

_ENTITY_QUERY_METHODS = frozenset({"find_by_entity_text_hash", "find_by_linked_memory_id"})

_ENTITY_OP_ACTIONS = {
    EntityOpType.INSERT: StorageAction.ADD,
    EntityOpType.LINK: StorageAction.UPDATE,
    EntityOpType.UNLINK_UPDATE: StorageAction.UPDATE,
    EntityOpType.DELETE: StorageAction.DELETE,
}


def _entity_scope(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """从 ``(space_id, filters)`` 近似重建授权用 Scope（有损，见代理类 docstring）。"""
    from jiuwen_memory.common.type_def import Scope

    space_id = args[0] if args and isinstance(args[0], str) else ""
    filters = kwargs.get("filters")
    return Scope(space=space_id, user=getattr(filters, "actor_id", None) or "")


def _entity_actions(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    """entity 方法 → StorageAction；batch 方法按 op 类型派生动作集（去重、定序）。

    ``find_*`` 归 SEARCH 而非 GET：既有 GET 是「按 id 点查 / 枚举」，把反向索引的批量
    反查归进去，会让只授了「读自己记录」的身份意外获得全库反查能力。
    ``execute_operations`` 不归 ADMIN：写入是常规数据面动作，授 ADMIN 等于解锁全部
    Store 的所有未映射方法；也不用固定的 {ADD,UPDATE,DELETE} 并集——纯 INSERT 的 batch
    会被迫要求 DELETE 权限。空 batch 返回空元组（零次 authorize），因为它不执行任何动作。
    """
    if name in _ENTITY_QUERY_METHODS:
        return (StorageAction.SEARCH,)
    if name != "execute_operations":
        return (StorageAction.ADMIN,)  # ensure_index 及未来的 DDL 方法
    operations = args[1] if len(args) > 1 else kwargs.get("operations") or []
    actions = {_ENTITY_OP_ACTIONS.get(op.type, StorageAction.ADMIN) for op in operations}
    return tuple(sorted(actions, key=lambda action: action.value))


class _AuthorizedEntityStoreProxy:
    """ENTITY 端口的授权代理：适配 ``space_id: str`` 首参与 entity 专属方法名。

    **scope 是有损近似**：entity 方法只带 ``space_id``（= ``space_id_from_scope`` 的算
    值：space → org → 字面量 ``"default"`` 三级降级）与部分方法的 ``filters.actor_id``
    （= ``scope.user``），无法无损反推原 Scope。本代理交给 ``StorageSecurity.authorize``
    的 Scope 只有 ``space`` / ``user`` 两段有意义：``space`` 可能实际是 org id 或字面量
    ``"default"``；``org`` / ``agent`` / ``session`` 恒为空；``execute_operations`` 无
    ``filters`` 参数，故写入侧 ``user`` 也恒为空（写入侧的 actor 隔离由
    ``EntityRecord.filters`` 记录内字段承担，不由授权入参承担）。自定义 StorageSecurity
    **不得**对 ``resource == "entity"`` 的调用按 ``org`` / ``agent`` / ``session`` 判定，
    应把 ``(space, user)`` 当作不透明的 routing/actor 二元组配合 ``action`` 使用。
    """

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
            scope = _entity_scope(args, kwargs)
            for action in _entity_actions(name, args, kwargs):
                self._security.authorize(access, scope, action, self._resource)
            return member(*args, **kwargs)

        return authorized


def _proxy_for(capability: StorageCapability, store: Any, security: StorageSecurity) -> Any:
    """ENTITY 端口用专用代理（space_id 首参 + entity 方法名映射）；其余六类走通用代理。"""
    if capability is StorageCapability.ENTITY:
        return _AuthorizedEntityStoreProxy(store, security, capability.value)
    return _AuthorizedStoreProxy(store, security, capability.value)


def _as_ports(value: Any) -> dict[str, Any]:
    """把端口入参归一为 ``{name: store}`` 表。

    单实例是 ``{"default": store}`` 的语法糖（手工构造/测试的常见形态）；传 dict 则
    原样收下，``"default"`` 只是其中一个普通键——本类不再把默认端口与具名端口分成
    两个入参，端口表是唯一事实来源（``capabilities()`` 与 ``has_*()`` 同源推导）。

    值为 None 的条目在此丢弃：端口值非 None 是本类的不变量（``health()`` 直接
    ``store.security`` / ``store.health()``，授权代理也假定非 None）。增强层 builder
    （如 entity 的 ES 实现在 hosts 未配时）约定返 None 表示「未配即降级关闭」。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {name: port for name, port in value.items() if port is not None}
    return {"default": value}


class CompositeStoreManager(StoreManager):
    """默认管理面实现：组合七类 Store + 端口代理 + 健康聚合。

    数据面领域操作（add/recall/...）由 :meth:`domain_store` 返回的
    :class:`~storage.domain_store.DomainStore` 承担；本类只管端口暴露与授权代理。

    两条入口：:meth:`__init__` 收**已构造的 Store 实例**（手工接线与测试路径）；
    :meth:`from_config` 从装配配置内部构造全部 Store 与默认数据面（装配路径，
    ``_build`` 即其一行封装）。
    """

    def __init__(
        self,
        *,
        kv: KVStore | dict[str, KVStore] | None = None,
        vector: VectorStore | dict[str, VectorStore] | None = None,
        fulltext: FulltextStore | dict[str, FulltextStore] | None = None,
        graph: GraphStore | dict[str, GraphStore] | None = None,
        fusion: FusionStore | dict[str, FusionStore] | None = None,
        fs: FSStore | dict[str, FSStore] | None = None,
        entity: EntityStore | dict[str, EntityStore] | None = None,
        security: StorageSecurity | None = None,
    ) -> None:
        self._named_stores: dict[StorageCapability, dict[str, Any]] = {
            StorageCapability.KV: _as_ports(kv),
            StorageCapability.VECTOR: _as_ports(vector),
            StorageCapability.FULLTEXT: _as_ports(fulltext),
            StorageCapability.GRAPH: _as_ports(graph),
            StorageCapability.FUSION: _as_ports(fusion),
            StorageCapability.FS: _as_ports(fs),
            StorageCapability.ENTITY: _as_ports(entity),
        }
        # capability = 该类存储至少有一个端口可用（与 has_*() 同源，二者不会分叉）。
        self._capabilities = frozenset(
            capability for capability, ports in self._named_stores.items() if ports
        )
        self._security = security or AllowAllStorageSecurity()
        self._proxies = {
            capability: {
                name: _proxy_for(capability, store, self._security)
                for name, store in ports.items()
            }
            for capability, ports in self._named_stores.items()
        }
        # 命名数据面实例表：装配路径由 from_config 构建并注入（default 自动构建
        # + domain_stores 配置段逐项构建）；未绑定名字的 domain_store(name) 报错
        # （手工构造场景需显式 bind_domain_store）。
        self._domain_stores: dict[str, DomainStore] = {}

    @property
    def security(self) -> StorageSecurity:
        return self._security

    @classmethod
    def from_config(cls, config: Any) -> CompositeStoreManager:
        """装配入口：内部构造全部 Store 与默认数据面，返回就绪的 manager。

        顺序是刚性的——数据面持有 manager 引用，故必须**先**把七类 Store 装齐、
        manager 自身构造完成，**最后**才构造 ``CompositeDomainStore`` 并注入：

        1. 七类 Store 全量聚合（:func:`_collect_ports`）→ 构造 manager；
        2. 预注册进具名缓存——召回路 builder 会经 ``StoreManagerProducer.resolve``
           回取本实例，不先注册就会递归新建第二套 manager（F06 双分支：具名构建用
           ``config.name``，匿名构建用含 ``id()`` 的合成名）；
        3. :func:`_build_domain_stores` 按 ``domain_stores`` 段逐套构造并绑定
           （``default`` 必建；每套自带检索 profile 与召回路）。

        本方法**不校验任何 capability 的必需性**：manager 的职责是如实报告装配出的
        能力，「哪类存储不可或缺」是消费方的约束（数据面缺 KV 真源时，其领域方法自会
        抛 ``UnsupportedStorageCapabilityError``）。这也让两条入口行为一致——
        :meth:`__init__` 同样允许构造不含 KV 的 manager（分层索引专用装配即如此）。
        """
        manager = cls(
            kv=_collect_ports(KvProducer, config),
            vector=_collect_ports(VectorProducer, config),
            fulltext=_collect_ports(FulltextProducer, config),
            graph=_collect_ports(GraphProducer, config),
            fusion=_collect_ports(FusionProducer, config),
            fs=_collect_ports(FsProducer, config),
            entity=_collect_ports(EntityStoreProducer, config),
        )
        if config.name:
            storage_ref = config.name
        else:
            storage_ref = f"__anon_store_manager_{id(manager)}__"
        StoreManagerProducer.put(storage_ref, manager)

        _build_domain_stores(config, manager, storage_ref)
        return manager

    def capabilities(self) -> frozenset[StorageCapability]:
        return self._capabilities

    def domain_store(self, name: str = "default") -> DomainStore:
        try:
            return self._domain_stores[name]
        except KeyError as exc:
            raise UnsupportedStorageCapabilityError(
                f"domain_store is not available: {name!r}"
            ) from exc

    def has_domain_store(self, name: str = "default") -> bool:
        return name in self._domain_stores

    def bind_domain_store(self, domain_store: DomainStore, name: str = "default") -> None:
        """注入命名数据面实例（manager 工厂装配末尾调用；手工构造场景显式调用）。

        多套命名数据面共享同一物理 Store 集，差异仅在检索 profile；同名重复绑定
        覆盖（手工接线口，非热切换入口）。
        """
        self._domain_stores[name] = domain_store

    def kv(self, name: str = "default") -> KVStore:
        return cast(KVStore, self._port(StorageCapability.KV, name))

    def vector(self, name: str = "default") -> VectorStore:
        return cast(VectorStore, self._port(StorageCapability.VECTOR, name))

    def fulltext(self, name: str = "default") -> FulltextStore:
        return cast(FulltextStore, self._port(StorageCapability.FULLTEXT, name))

    def graph(self, name: str = "default") -> GraphStore:
        return cast(GraphStore, self._port(StorageCapability.GRAPH, name))

    def fusion(self, name: str = "default") -> FusionStore:
        return cast(FusionStore, self._port(StorageCapability.FUSION, name))

    def fs(self, name: str = "default") -> FSStore:
        return cast(FSStore, self._port(StorageCapability.FS, name))

    def entity(self, name: str = "default") -> EntityStore:
        return cast(EntityStore, self._port(StorageCapability.ENTITY, name))

    def has_kv(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.KV, name)

    def has_vector(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.VECTOR, name)

    def has_fulltext(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FULLTEXT, name)

    def has_graph(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.GRAPH, name)

    def has_fusion(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FUSION, name)

    def has_fs(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.FS, name)

    def has_entity(self, name: str = "default") -> bool:
        return self._has_port(StorageCapability.ENTITY, name)

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


def _collect_ports(producer: type[Factory], config: Any) -> dict[str, Any]:
    """全量聚合：该类 Store 命名空间下**所有**具名实例（含 ``default``）都成为端口。

    声明即端口——消费者经 ``manager.xxx(name)`` 直接可达，manager 侧不再列白名单，
    也不再用 params 引用键单独指定默认端口：``default`` 只是端口表里的一个普通键，
    即命名空间中名为 ``"default"`` 的实例。

    这消除了「manager params 漏写 ``<ns>_store`` 引用键 → 该能力静默消失」的陷阱：
    配置合并是实例级整体覆盖（``AssemblyContext.merged`` 按实例名 update 整个
    ``RawSpec``，无 params 深合并），部署一旦覆写 ``store_manager.default.params``
    就得全量抄写其全部键，漏一个即静默丢能力。改由命名空间推导后，声明了后端就有
    对应端口，与「是否启用某条链路」（``vector_enabled`` 等开关）解耦。

    **builder 返 None 的实例被丢弃**：增强层后端（如 entity_store 的 ES 实现在
    ``hosts`` 未配时）约定返 None 表示「未配即降级关闭」。端口表的值非 None 是
    :class:`CompositeStoreManager` 的不变量（``health()`` 直接 ``store.security`` /
    ``store.health()``，代理也假定非 None），故在这个**唯一的**「命名空间 → 端口」
    构造点上统一过滤，而不是把 None 判断散进 health/代理。对其余六类是 no-op
    （它们的 builder 缺必填参一律 ``require_param`` 抛错，不返 None）。

    注意：encrypted KV 的明文 raw 若以具名声明（``raw_kv_store: <name>``），也会随之
    暴露为端口；raw 推荐 inline 声明（F04/S06 文档约定）。
    """
    namespace = config.ctx.namespaces.get(producer.TOP_NAME, {})
    ports: dict[str, Any] = {}
    for name in namespace:
        store = producer.build_named(name, config.ctx)
        if store is None:
            logger.warning(
                "%s.%s 未装配（builder 返 None，通常是必填连接参数未配），该端口丢弃",
                producer.TOP_NAME,
                name,
            )
            continue
        ports[name] = store
    return ports


def _build_domain_stores(config: Any, manager: CompositeStoreManager, storage_ref: str) -> None:
    """装配全部命名数据面（含 ``default``），逐个绑定进 manager。

    数据面的全部装配参数都住在 ``store_manager.<inst>.params.domain_stores.<name>``：
    检索首选路径、真源 KV 端口名、``domain_store_target`` 与七个 ``*_recaller`` 选择键。
    manager 段自身不承载数据面配置——七类 Store 端口由 :func:`_collect_ports` 扫命名
    空间得来，与 params 无关。

    **继承规则只有一条，对所有键统一生效**：``default`` entry 是 base，命名 entry
    overlay 在其上；再经 ``ComponentConfig.get`` 回退 ``globals``（``vector_enabled``
    等跨切面开关走这条）。base 不是便利而是必需——``Factory.dep`` 读 ``config.params``
    直读不回退 globals，命名 entry 漏掉 ``*_recaller`` 键会让 ``dep`` 匿名新建一套
    不共享的 recaller。

    ``domain_stores`` 整段缺省时 base 为空，仍建出全默认的 ``default`` 数据面。
    ``composite``（默认 target）走 :meth:`CompositeDomainStore.for_manager` 直接构造
    ——manager 手里已有自身引用，无须再经 Producer 绕一圈按名回取；其余 target 仍走
    ``DomainStoreProducer``（可换实现能力保留），靠上游已完成的具名预注册让 builder
    内的 ``dep`` 命中缓存。
    """
    entries = config.params.get("domain_stores") or {}
    if not isinstance(entries, dict):
        raise ValidationError(
            "store_manager params.domain_stores 必须是 {<name>: {<数据面参数>...}} 的映射，"
            f"got {type(entries).__name__}"
        )
    for ds_name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValidationError(
                f"store_manager params.domain_stores.{ds_name} 必须是映射，"
                f"got {type(entry).__name__}"
            )
    base = entries.get("default") or {}
    for ds_name in ("default", *(name for name in entries if name != "default")):
        params = base if ds_name == "default" else {**base, **entries[ds_name]}
        target = params.get("domain_store_target", "composite")
        if target == "composite":
            # ds_config.name 取 manager 的实例名：_assemble_recallers 的具名/匿名双
            # 分支据此判断能否走 recaller 命名空间的具名引用，不能改成数据面名。
            ds_config = ComponentConfig(
                params=params, ctx=config.ctx, target=config.target, name=config.name
            )
            domain_store: Any = CompositeDomainStore.for_manager(manager, ds_config)
        else:
            domain_store = DomainStoreProducer.build(
                target, {**params, "store_manager": storage_ref}, config.ctx
            )
        manager.bind_domain_store(domain_store, ds_name)


@StoreManagerProducer.register("composite")
def _build(config):
    """YAML 装配入口：一行封装 :meth:`CompositeStoreManager.from_config`。"""
    return CompositeStoreManager.from_config(config)
