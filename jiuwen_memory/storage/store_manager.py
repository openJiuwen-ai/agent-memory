# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""存储管理面契约：能力发现、命名端口暴露、统一授权代理、健康聚合。

F07 把原统一 ``Storage`` ABC 拆为管理面 :class:`StoreManager` 与数据面
:class:`~storage.domain_store.DomainStore` 两个独立 ABC（分处不同文件）。上层需要底层
Store 实例（写索引、读 KV 真源等）的组件依赖本接口；数据面领域操作
（add/recall/...）由 ``domain_store(name)`` 返回的 DomainStore 承担。

端口接口为**单一入口**：每个 capability 一对 ``xxx(name="default")`` /
``has_xxx(name="default")`` 方法，无 property 快捷方式、无 ``*_port`` 后缀双入口
（二者是 F05 首版遗留的重复写法，F07 合并）。默认端口 ``manager.kv()``；分层索引起
命名端口 ``manager.fulltext("layers_l0")``。命名端口装配为**命名空间全量自动**：
七类 ``*_store`` 命名空间下所有非 ``default`` 具名实例都成为端口（声明即端口）。

ENTITY 端口（F07-D）是第七 capability，与其余六类同构暴露；两处例外须知：其一，方法首
入参是 ``space_id: str`` 而非 ``Scope``（BaseStore「scope 显式第一入参」的唯一例外），
授权由 ``_AuthorizedEntityStoreProxy`` 专门适配；其二，默认端口额外接受
「``entity_store.default`` 声明即端口」的兜底解析（理由见 ``composite_store_manager``
的 ``_entity_store``）。

全局唯一 manager（F08）：进程内共享一个 StoreManager，由 ``globals.store_manager``
指名（值 = store_manager 命名空间下的实例名）。消费者不再逐个声明 ``storage``
引用，统一经 :class:`StoreManagerProducer.resolve` 解析。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory

from .domain_store import DomainStore
from .entity_store import EntityStore
from .fs import FSStore
from .fulltext import FulltextStore
from .fusion import FusionStore
from .graph import GraphStore
from .kv import KVStore
from .security import StorageSecurity
from .vector import VectorStore


class StoreManagerProducer(Factory):
    """统一 StoreManager 的注册式工厂。

    TOP_NAME 为 ``"store_manager"``（F08 起顶层配置段从 ``storage:`` 更名
    ``store_manager:``，消除「配置段名 storage vs 全局指名键 store_manager」的分裂）。
    默认具名实例为 ``store_manager.default``。
    """

    TOP_NAME = "store_manager"

    @classmethod
    def resolve(cls, config) -> StoreManager:
        """解析全局共享 StoreManager：params 覆盖 → ``globals.store_manager`` → default。

        全局唯一 manager 语义（F08）：指名实例须在 store_manager 命名空间声明，或经
        ``put`` 预置（F02 ``RoutingStoreManager`` 手工注入命中缓存）；未声明抛
        :class:`~common.errors.ValidationError`——不做匿名兜底构建，避免出现绕开
        全局指名的第二套 manager 实例。manager 内部装配（recaller synthetic
        自接线）经 params 显式覆盖注入引用，仍走具名缓存命中。
        """
        name = config.get("store_manager", "default")
        if not isinstance(name, str) or not name:
            raise ValidationError(
                "StoreManagerProducer.resolve: store_manager 引用必须是"
                " store_manager 命名空间下的非空实例名"
                f"（params.store_manager / globals.store_manager），got {name!r}"
            )
        manager = cls.build_named(name, config.ctx)
        if not isinstance(manager, StoreManager):
            raise TypeError(
                f"StoreManagerProducer assembled {type(manager).__name__}, "
                "expected StoreManager"
            )
        return manager


def resolve_name(config, key: str, *, default: str = "default") -> str:
    """读消费者的具名 Store/数据面选择键（params 直读，缺省 default）。

    键名与命名空间名一致（``kv_store`` / ``vector_store`` / ``fulltext_store`` /
    ``graph_store`` / ``domain_store`` ...），值为 manager 端口/数据面名。**params
    直读不回退 globals**——端口选择是消费者实例级决策，不是跨切面开关。只接受
    字符串：inline dict 是 Factory 依赖引用语义（绕开 manager 自建实例），此处
    不支持，装配期报错。
    """
    name = config.params.get(key, default)
    if not isinstance(name, str) or not name:
        raise ValidationError(
            f"store_manager 端口选择键 {key!r} 必须是 manager 端口/数据面的非空"
            f"实例名字符串（不支持 inline dict），got {name!r}"
        )
    return name


class StorageCapability(str, Enum):
    KV = "kv"
    VECTOR = "vector"
    FULLTEXT = "fulltext"
    GRAPH = "graph"
    FUSION = "fusion"
    FS = "fs"
    ENTITY = "entity"


class StoreManager(ABC):
    """存储管理面契约：能力发现、命名端口暴露、统一授权代理、健康聚合。

    端口方法返回经 ``StorageSecurity`` 授权代理的完整 Store 契约；未声明端口访问统一抛
    :class:`~common.errors.UnsupportedStorageCapabilityError`。``domain_store(name)``
    返回本 manager 管理的**命名数据面**实例——多套命名数据面共享同一物理 Store 集，
    差异仅在检索 profile（``preferred_retrieval_pipeline`` + recallers 组合）；实现需
    保证同名多次调用返回同一实例（``bind_recallers`` 状态不丢）。
    """

    @property
    @abstractmethod
    def security(self) -> StorageSecurity:
        ...

    @abstractmethod
    def capabilities(self) -> frozenset[StorageCapability]:
        ...

    @abstractmethod
    def domain_store(self, name: str = "default") -> DomainStore:
        """取命名数据面实例；实现需缓存（同名多次调用返回同一实例）。"""

    @abstractmethod
    def has_domain_store(self, name: str = "default") -> bool:
        """命名数据面是否已装配（含 default）。"""
        ...

    @abstractmethod
    def kv(self, name: str = "default") -> KVStore:
        ...

    @abstractmethod
    def vector(self, name: str = "default") -> VectorStore:
        ...

    @abstractmethod
    def fulltext(self, name: str = "default") -> FulltextStore:
        ...

    @abstractmethod
    def graph(self, name: str = "default") -> GraphStore:
        ...

    @abstractmethod
    def fusion(self, name: str = "default") -> FusionStore:
        ...

    @abstractmethod
    def fs(self, name: str = "default") -> FSStore:
        ...

    @abstractmethod
    def entity(self, name: str = "default") -> EntityStore:
        """取 ENTITY 端口（实体反向索引）。

        注意首入参例外：``EntityStore`` 的方法以 ``space_id: str`` 而非 ``Scope``
        作第一入参（见 :mod:`storage.entity_store`），端口本身的取用方式与其余六类一致。
        """

    def has_kv(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.KV in self.capabilities()

    def has_vector(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.VECTOR in self.capabilities()

    def has_fulltext(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.FULLTEXT in self.capabilities()

    def has_graph(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.GRAPH in self.capabilities()

    def has_fusion(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.FUSION in self.capabilities()

    def has_fs(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.FS in self.capabilities()

    def has_entity(self, name: str = "default") -> bool:
        return name == "default" and StorageCapability.ENTITY in self.capabilities()

    @abstractmethod
    def health(self) -> None:
        ...
