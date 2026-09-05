# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CompositeStoreManager — 默认管理面实现。

组合七类 Store + 端口代理 + 健康聚合；数据面实例由 manager 工厂内的
``DomainStoreProducer.build`` 构建并经 :meth:`bind_domain_store` 注入（装配链路见
模块尾部 ``_build``）。召回路装配在 manager ``_build`` 末尾完成（F06 内收设计保留）：
按 ``vector_enabled`` / ``graph_enabled`` / ``layers_index_enabled`` 与 ``*_recaller``
配置同步组装，装配错误 fail-fast。

命名数据面（F08）：``domain_store(name)`` 多槽——``default`` 之外可经
``store_manager.<inst>.params.domain_stores`` 声明任意命名数据面（差异 = 检索
profile：``preferred_retrieval_pipeline`` + recaller 选择键覆盖）；各套共享同一
物理 Store 集。

命名端口（F08）：七类 ``*_store`` 命名空间下所有非 ``default`` 具名实例**全量自动**
成为端口（声明即端口）；encrypted 的明文 raw 若以具名声明会随之暴露，raw 推荐
inline 声明（见 F04/S06）。

recaller builder 会经 ``StoreManagerProducer.resolve`` 回取本 manager 实例，故工厂
先把构建中的实例预注册进具名缓存再组装召回路，打破循环依赖：具名构建用
``config.name`` 预注册；匿名构建用合成名（``id(manager)`` 唯一）预注册并把 manager
引用注入 recaller params。详见 F06/F07/F08 特性文档。
"""

from __future__ import annotations

from typing import Any, cast

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def.entity import EntityOpType
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.storage.domain_store import DomainStore, DomainStoreProducer
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


# -- ENTITY 端口的授权适配（F07-D）------------------------------------------------ #
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


class CompositeStoreManager(StoreManager):
    """默认管理面实现：组合七类 Store + 端口代理 + 健康聚合。

    数据面领域操作（add/recall/...）由 :meth:`domain_store` 返回的
    :class:`~storage.domain_store.DomainStore` 承担；本类只管端口暴露与授权代理。
    """

    def __init__(
        self,
        *,
        kv: KVStore | None = None,
        vector: VectorStore | None = None,
        fulltext: FulltextStore | None = None,
        graph: GraphStore | None = None,
        fusion: FusionStore | None = None,
        fs: FSStore | None = None,
        entity: EntityStore | None = None,
        kv_ports: dict[str, KVStore] | None = None,
        vector_ports: dict[str, VectorStore] | None = None,
        fulltext_ports: dict[str, FulltextStore] | None = None,
        graph_ports: dict[str, GraphStore] | None = None,
        fusion_ports: dict[str, FusionStore] | None = None,
        fs_ports: dict[str, FSStore] | None = None,
        entity_ports: dict[str, EntityStore] | None = None,
        security: StorageSecurity | None = None,
    ) -> None:
        self._stores = {
            StorageCapability.KV: kv,
            StorageCapability.VECTOR: vector,
            StorageCapability.FULLTEXT: fulltext,
            StorageCapability.GRAPH: graph,
            StorageCapability.FUSION: fusion,
            StorageCapability.FS: fs,
            StorageCapability.ENTITY: entity,
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
            StorageCapability.ENTITY: entity_ports,
        }
        self._named_stores: dict[StorageCapability, dict[str, Any]] = {}
        for capability, store in self._stores.items():
            # 端口值非 None 是本类的不变量：health() 直接 store.security / store.health()，
            # 授权代理也假定非 None。增强层 builder（如 entity 的 ES 实现在 hosts 未配时）
            # 约定返 None 表示「未配即降级关闭」，在此统一丢弃。
            ports = {
                name: port
                for name, port in (configured_ports.get(capability) or {}).items()
                if port is not None
            }
            if store is not None:
                ports["default"] = store
            self._named_stores[capability] = ports
        self._security = security or AllowAllStorageSecurity()
        self._proxies = {
            capability: {
                name: _proxy_for(capability, store, self._security)
                for name, store in ports.items()
            }
            for capability, ports in self._named_stores.items()
        }
        # 命名数据面实例表：由 _build 工厂经 bind_domain_store 注入（default 自动构建
        # + domain_stores 配置段逐项构建）；未绑定名字的 domain_store(name) 报错
        # （手工构造场景需显式绑定）。
        self._domain_stores: dict[str, DomainStore] = {}

    @property
    def security(self) -> StorageSecurity:
        return self._security

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


def _optional_store(producer: type[Factory], config: Any, field: str) -> Any | None:
    """读 manager params 的可选 Store 引用；未声明即无该能力（不静默补默认实例）。"""
    if field not in config.params:
        return None
    return producer.dep(config, field)


def _manager_kv(config: Any) -> Any:
    """读 manager params 的 KV 引用；缺键共享 ``kv_store.default`` 具名实例。

    缺键时匿名新建 memory KV 会与 ``kv_store.default`` 具名实例静默分裂成两套
    真源（绕开 manager 的消费方如 evolver ``message_store`` 拿到的是具名实例），
    故缺键改走 ``build_named("default")`` 命中具名缓存——与命名空间共享拓扑一致；
    未声明 ``kv_store.default`` 时由 ``build_named`` 抛 ``ValidationError``。
    """
    if "kv_store" not in config.params:
        return KvProducer.build_named("default", config.ctx)
    return KvProducer.dep(config)


def _entity_store(config: Any) -> Any | None:
    """ENTITY 端口三级解析：params 显式引用 → ``entity_store.default`` 具名实例 → 无能力。

    与 :func:`_manager_kv` 同构（params 缺键回退 default 具名实例），差别在必需性：
    KV 缺 default 抛错，entity 缺整个命名空间即无该能力（增强层，未配即关，不报错）。

    为何不像 vector/graph/fusion/fs 那样只认 params 引用键：配置合并是**实例级整体
    覆盖**（``AssemblyContext.merged`` 按实例名 update 整个 RawSpec，无 params 深合并），
    要求 params 引用就等于强制每个既有部署在自己的 config.yml 里全量抄写 ``defaults``
    的 ``store_manager.default.params``（kv/vector/fulltext/graph +
    ``preferred_retrieval_pipeline`` + 7 个 ``*_recaller`` 键），漏抄一个 recaller 键 =
    一路召回静默消失。ENTITY 是后加的第七能力，迁移成本与漂移风险高于一致性收益；
    受管成员也并非都由 params 引用键驱动（同层级的 ``domain_store`` 就是 manager 工厂
    内部构建 + ``bind_domain_store`` 注入）。将来 ``defaults`` 若声明
    ``entity_store: _D``（默认栈有了内存 entity 实现时），第一级自然命中，本兜底退居后备。
    """
    if "entity_store" in config.params:
        return EntityStoreProducer.dep(config, "entity_store")
    if "default" in config.ctx.namespaces.get(EntityStoreProducer.TOP_NAME, {}):
        return EntityStoreProducer.build_named("default", config.ctx)
    return None


def _named_ports(producer: type[Factory], config: Any) -> dict[str, Any]:
    """命名端口全量自动：该类 Store 命名空间下**所有非 default** 具名实例都成为端口。

    声明即端口——消费者经 ``manager.xxx(name)`` 直接可达，无需 manager 侧再列白名单。
    注意：encrypted KV 的明文 raw 若以具名声明（``raw_kv_store: <name>``），也会随之
    暴露为端口；raw 推荐 inline 声明（F04/S06 文档约定）。

    **builder 返 None 的实例被丢弃**：增强层后端（如 entity_store 的 ES 实现在
    ``hosts`` 未配时）约定返 None 表示「未配即降级关闭」。端口表的值非 None 是
    :class:`CompositeStoreManager` 的不变量（``health()`` 直接 ``store.security`` /
    ``store.health()``，代理也假定非 None），故在这个**唯一的**「命名空间 → 端口」
    构造点上统一过滤，而不是把 None 判断散进 health/代理。对六类 Store 是 no-op
    （它们的 builder 缺必填参一律 ``require_param`` 抛错，不返 None）。
    """
    namespace = config.ctx.namespaces.get(producer.TOP_NAME, {})
    ports: dict[str, Any] = {}
    for name in namespace:
        if name == "default":
            continue
        store = producer.build_named(name, config.ctx)
        if store is None:
            logger.warning(
                "%s.%s 未装配（builder 返 None，通常是必填连接参数未配），该命名端口丢弃",
                producer.TOP_NAME,
                name,
            )
            continue
        ports[name] = store
    return ports


def _named_domain_stores(
    config: Any, manager: CompositeStoreManager, storage_ref: str
) -> None:
    """装配 ``store_manager.<inst>.params.domain_stores`` 声明的命名数据面（F08）。

    每套命名数据面共享本 manager 的物理 Store 集，差异仅在检索 profile：
    ``preferred_retrieval_pipeline`` / ``domain_store_target`` 与 ``*_recaller``
    选择键、``vector_enabled`` 等开关按 entry 覆盖（overlay 在 manager 原始
    params 之上）。段内不允许声明 ``"default"``——default 由装配自动构建，
    显式声明属于歧义配置，装配期报错。
    """
    from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore

    entries = config.params.get("domain_stores") or {}
    if not isinstance(entries, dict):
        raise ValidationError(
            "store_manager params.domain_stores 必须是 {<name>: {<覆盖键>...}} 的映射，"
            f"got {type(entries).__name__}"
        )
    if "default" in entries:
        raise ValidationError(
            'store_manager params.domain_stores 不允许声明 "default" 键'
            "（default 数据面由装配自动构建，显式声明产生歧义）"
        )
    for ds_name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValidationError(
                f"store_manager params.domain_stores.{ds_name} 必须是映射，"
                f"got {type(entry).__name__}"
            )
        ds_params: dict[str, Any] = {"store_manager": storage_ref}
        for key in ("preferred_retrieval_pipeline", "domain_store_target"):
            if key in entry:
                ds_params[key] = entry[key]
        named_ds = DomainStoreProducer.build(
            ds_params.pop("domain_store_target", "composite"), ds_params, config.ctx
        )
        if isinstance(named_ds, CompositeDomainStore):
            from jiuwen_memory.storage.domain_store_impl.composite_domain_store import (
                _assemble_recallers,
            )

            overlay = ComponentConfig(
                params={**config.params, **entry},
                ctx=config.ctx,
                target=config.target,
                name=config.name,
            )
            named_ds.bind_recallers(_assemble_recallers(overlay, storage=manager))
        manager.bind_domain_store(named_ds, ds_name)


@StoreManagerProducer.register("composite")
def _build(config):
    from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore

    manager = CompositeStoreManager(
        kv=_manager_kv(config),
        vector=_optional_store(VectorProducer, config, "vector_store"),
        fulltext=_optional_store(FulltextProducer, config, "fulltext_store"),
        graph=_optional_store(GraphProducer, config, "graph_store"),
        fusion=_optional_store(FusionProducer, config, "fusion_store"),
        fs=_optional_store(FsProducer, config, "fs_store"),
        entity=_entity_store(config),
        kv_ports=_named_ports(KvProducer, config),
        vector_ports=_named_ports(VectorProducer, config),
        fulltext_ports=_named_ports(FulltextProducer, config),
        graph_ports=_named_ports(GraphProducer, config),
        fusion_ports=_named_ports(FusionProducer, config),
        fs_ports=_named_ports(FsProducer, config),
        entity_ports=_named_ports(EntityStoreProducer, config),
    )
    # 预注册打破循环（F06 双分支）：具名构建用 config.name；匿名构建用合成名。
    # domain builder 经 StoreManagerProducer.dep 字符串引用命中此缓存。
    if config.name:
        storage_ref = config.name
        StoreManagerProducer.put(config.name, manager)
    else:
        storage_ref = f"__anon_store_manager_{id(manager)}__"
        StoreManagerProducer.put(storage_ref, manager)
    # 数据面经 DomainStoreProducer 构建（target 可经 domain_store_target 覆盖），
    # preferred_retrieval_pipeline 透传（该键在 store_manager 段 params，不在
    # globals，新造的 domain config 读不到，须显式传）。
    ds_params: dict[str, Any] = {"store_manager": storage_ref}
    if "preferred_retrieval_pipeline" in config.params:
        ds_params["preferred_retrieval_pipeline"] = config.get("preferred_retrieval_pipeline")
    domain_store = DomainStoreProducer.build(
        config.get("domain_store_target", "composite"), ds_params, config.ctx
    )
    # 召回装配内收（F06）：读 manager 原始 config（vector_enabled 等开关键在
    # globals / storage params，domain 的新造 config 读不全），绑定在 domain 侧。
    if isinstance(domain_store, CompositeDomainStore):
        from jiuwen_memory.storage.domain_store_impl.composite_domain_store import (
            _assemble_recallers,
        )

        domain_store.bind_recallers(_assemble_recallers(config, storage=manager))
    manager.bind_domain_store(domain_store)
    # 命名数据面（F08）：domain_stores 段逐项构建（default 已由上方自动构建）。
    _named_domain_stores(config, manager, storage_ref)
    return manager
