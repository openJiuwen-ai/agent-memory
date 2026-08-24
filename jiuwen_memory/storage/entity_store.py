# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityStore — 实体反向索引存储端口（与 VectorStore/FulltextStore 平级）。

抽象实体索引的读写能力：hash 精确查询、bulk 变更、反查关联。实现侧
（``ElasticsearchEntityStore``）走 Elasticsearch，后续可换其他后端。

迁移自原 ``core.ports.entity_store``，签名做两处改造：
- ``space_id`` 从 ``UUID`` 改 ``str``（当前工程 routing 不要求 UUID，存
  ``space_id_from_scope`` 的 str 算值）
- ``find_by_linked_memory_id`` 的 ``memory_id`` 从 ``UUID`` 改 ``str``（存 unit.id）

入参与 ``BaseStore`` 对齐——但 entity 索引不走 scope 原生隔离（它用 space_id
routing + ``EntityStoreFilters`` actor 单段 term），故这里 space_id 作显式参数，不用
Scope。理由：entity 索引的隔离维度（space_id routing + actor_id term）和
VectorStore 的 scope 五段隔离模型不同，强行用 Scope 会丢 space_id 的 routing
语义。agent/session 不作隔离维度：实体是 user 级知识，同 user 下跨 agent、跨
session 共享。

**2026-08-12 改造**：归并退化为 hash 精确 only，砍掉向量 kNN 检索能力。
``search``（向量 kNN）方法删除，entity 索引不再存向量、不再依赖 Embedder。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityRecord,
    EntityStoreFilters,
)
from jiuwen_memory.common.factory.factory import Factory

from .base import BaseStore


class EntityStoreProducer(Factory):
    """EntityStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 elasticsearch）。各实现在 ``entity_impl`` 下以
    ``@EntityStoreProducer.register("<后端>")`` 自注册——注册发生在 import
    实现模块时，由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "entity_store"


class EntityStore(BaseStore):
    """实体反向索引存储抽象。space_id 作显式第一入参（与 VectorStore/FulltextStore 的 scope 模式不同，见模块 docstring）。"""

    @abstractmethod
    def ensure_index(self) -> None:
        """确保索引已创建并就绪。使用前必须调一次，否则后续查询抛 not ready。"""

    @abstractmethod
    def find_by_entity_text_hash(
        self,
        space_id: str,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        limit: int = 500,
    ) -> list[EntityRecord]:
        """按 entity_text_hash keyword term 查询，返回命中的实体记录。"""

    @abstractmethod
    def find_by_linked_memory_id(
        self,
        space_id: str,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
    ) -> list[EntityRecord]:
        """反查：哪些实体关联了该 memory_id（unlink 用）。

        filters 复用写入侧的 actor_id 隔离维度——unlink 只命中调用方 scope
        所属的实体文档，避免 space 内跨 user 的孤立误删（纵深防御：当前
        unit.id 是 UUID4 全局唯一不会撞，但把隔离下沉到存储层后，即便未来
        出现非 UUID 的 id 路径也安全）。
        """

    @abstractmethod
    def execute_operations(
        self,
        space_id: str,
        operations: list[EntityOperation],
    ) -> EntityBatchResult:
        """bulk 变更（INSERT/LINK/UNLINK_UPDATE/DELETE 混合），per-item 粒度返回。"""
