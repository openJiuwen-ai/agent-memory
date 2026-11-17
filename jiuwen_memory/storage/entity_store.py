# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityStore — 实体反向索引存储端口（与 VectorStore/FulltextStore 平级）。

F09 D9 起本端口与其它 Store 对齐：

- 所有方法首参由 ``space_id: str`` 改为 ``scope: Scope``（S06「Store 方法首参是
  scope」）；后端内部仍用 ``space_id_from_scope(scope)`` 计算 routing。
- ``find_by_entity_text_hash`` / ``search`` 接受可选 ``shards``；``None`` 表示全部分片，
  不支持分片的实现收到非空 ``shards`` 时必须抛「不支持」错误。
- 新增增量操作 ``UNLINK``（只移除关联、从不删除记录）与 ``DELETE_IF_EMPTY``
  （记录关联为空时原子删除）。实现的原子性由 ``StoreCapability.ATOMIC_UNLINK``
  声明；``shard_routed`` 包装层要求内层声明该能力。
- 分组关联能力用 ``StoreCapability.GROUPED_LINKS`` 声明：实现的 ``LINK`` / ``UNLINK``
  / ``INSERT`` 可接收每个关联 id 的所属分片，并在 ``shards`` 过滤时只返回选中分片的
  关联。``UNLINK`` 携带 ``memory_shards`` 时只从对应分组移除该关联；该 id 仍属于其它
  分组时保留在 ``linked_memory_ids`` 中（F09 D16 搬迁先追加目标分组、再解除源分组）。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityRecord,
    EntitySearchResult,
    EntityStoreFilters,
)

from .base import BaseStore


class EntityStoreProducer(Factory):
    """EntityStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 elasticsearch / memory）。各实现在 ``entity_impl`` 下以
    ``@EntityStoreProducer.register("<后端>")`` 自注册——注册发生在 import
    实现模块时，由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "entity_store"


class EntityStore(BaseStore):
    """实体反向索引存储抽象；首参为完整 ``Scope``（F09 D9）。

    ``CONTRACT_VERSION`` 是**契约版本**，不是实现版本：子类继承即表示已实现当前
    契约（``Scope`` 首参 + 增量 ``UNLINK`` / ``DELETE_IF_EMPTY``）。按旧签名（首参
    ``space_id: str``）编写的第三方实现没有这个属性，``legacy`` 适配器据此识别并
    包装——不靠 ``inspect.signature`` 猜参数名（S09 #9）。
    """

    #: 2 = Scope 首参 + 增量 UNLINK / DELETE_IF_EMPTY（F09 D9）。
    CONTRACT_VERSION = 2

    @abstractmethod
    def ensure_index(self) -> None:
        """确保索引已创建并就绪。使用前必须调一次，否则后续查询抛 not ready。"""

    @abstractmethod
    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        limit: int = 500,
    ) -> list[EntityRecord]:
        """按 entity_text_hash keyword term 查询，返回命中的实体记录。

        ``shards`` 非空时只返回具有选中分片关联的记录，关联视图也只含选中分片。
        """

    @property
    def supports_complete_linked_enumeration(self) -> bool:
        """Whether ``find_by_linked_memory_id(complete=True)`` truly enumerates all.

        Relocation must not treat a truncated / limited backend as complete. Default
        ``False`` keeps third-party plugins safe: they are never asked for complete
        enumeration unless they explicitly opt in.
        """
        return False

    @abstractmethod
    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        complete: bool = False,
    ) -> list[EntityRecord]:
        """反查：哪些实体关联了该 memory_id（unlink 用）。

        filters 复用写入侧的 actor_id 隔离维度——unlink 只命中调用方 scope
        所属的实体文档，避免 space 内跨 user 的孤立误删；``shards`` 语义同上。
        ``complete=True`` 表示**完整枚举**（搬迁必须用它）：实现不得静默截断，
        无法完整返回时应拒绝而不是返回部分结果；普通调用保持原限量语义。
        只有 ``supports_complete_linked_enumeration=True`` 的实现才会收到该参数。
        """

    @abstractmethod
    def execute_operations(
        self,
        scope: Scope,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        """bulk 变更（INSERT / LINK / UNLINK / DELETE_IF_EMPTY / DELETE 混合）。

        per-item 粒度返回：一条失败不影响其它条；响应里如实给出成功 / 失败 id。
        空记录不被 ``find_*`` / ``search`` 返回。
        """

    @abstractmethod
    def search(
        self,
        scope: Scope,
        query_vector: list[float],
        *,
        top_k: int,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
    ) -> list[EntitySearchResult]:
        """向量 kNN 检索实体记录；不支持时抛 ``UnsupportedCapabilityError``。"""
