# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""存储数据面契约：MemoryUnit 领域 CRUD 与检索适配。

F07 把原统一 ``Storage`` ABC 拆为管理面 :class:`~storage.store_manager.StoreManager`
（分处不同文件）与数据面 :class:`DomainStore` 两个独立 ABC。上层调用领域接口
（add/get/list/recall/...）的组件依赖本接口；端口管理（kv/vector/...）由 StoreManager
承担，通过 ``manager.domain_store()`` 取本实例。

``DomainStore`` 实现经 :class:`DomainStoreProducer` 注册装配（``domain_store`` 命名
空间，默认拓扑不声明该段——domain_store 由 manager 工厂内部构建，是装配下游步骤，
不是平级入口；详见 ``docs/features/storage/F07-storage-manager-domain-store-split.md``）。

``bind_recallers`` 不下沉到本 ABC：仅 :class:`~storage.domain_store_impl.CompositeDomainStore`
实现（手工/测试接线口）；``RoutingDomainStore`` 不实现（active 切换语义要求各预装
实例装配期各自绑定）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

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

from .security import StorageAccessContext, StorageSecurity
from .types import IndexRemoveMode, IndexWriteMode, MemoryListResult


class DomainStoreProducer(Factory):
    """DomainStore 的注册式工厂（``domain_store`` 命名空间）。

    装配链路：``StoreManagerProducer._build("composite")`` 先构建 manager 并预注册，
    再经本工厂 ``build`` 构建 domain_store 并 ``bind_domain_store``——manager 是唯一
    装配入口，本工厂不是平级 YAML 入口（默认拓扑不声明 ``domain_store:`` 段）。builder
    内经 ``StoreManagerProducer.dep(config)`` 回取 manager（字符串引用命中预注册缓存）。

    命名数据面（F08）：``store_manager.<inst>.params.domain_stores`` 段声明的每套
    命名数据面也经本工厂构建（同样回取 manager 引用），差异键（``preferred_retrieval_pipeline``
    / recaller 选择键）随 params 透传；段内不允许声明 ``"default"``。

    ``store_manager`` 参数必填、无 default：独立构建 domain_store 会触发 manager
    匿名重建的无限递归，且违背「所有存储类从 StoreManager 获取」原则——缺引用时
    fail-fast。
    """

    TOP_NAME = "domain_store"


class DomainStore(ABC):
    """存储数据面契约：MemoryUnit 领域 CRUD 与检索适配入口。

    实现内部需要 KV 真源时应通过注入的 manager 引用获取，不另持 KV 连接
    （遵循「所有存储类从 StoreManager 获取」原则）。
    """

    @property
    @abstractmethod
    def security(self) -> StorageSecurity:
        ...

    @abstractmethod
    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        ...

    @abstractmethod
    def scopes(self) -> list[Scope]:
        """返回当前存储内已有记忆数据的作用域。"""
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

        覆盖范围是**实现相关**的：该实现按其能力落地——``CompositeDomainStore`` 无投影
        能力，只落记忆本体；一体化后端可一次建立正排、倒排、向量、图。

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
