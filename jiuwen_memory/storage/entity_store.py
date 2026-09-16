# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EntityStore — 实体反向索引存储端口（与 VectorStore/FulltextStore 平级）。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
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

    ``name`` 即后端名（如 elasticsearch）。各实现在 ``entity_impl`` 下以
    ``@EntityStoreProducer.register("<后端>")`` 自注册——注册发生在 import
    实现模块时，由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "entity_store"


class EntityStore(BaseStore):
    """实体反向索引存储抽象。

    space_id 作显式第一入参（与 VectorStore/FulltextStore 的 scope 模式不同，
    见模块 docstring）。
    """

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

    @abstractmethod
    def search(self, space_id: str, query_vector: list[float], *, top_k: int,
               filters: EntityStoreFilters,) -> list[EntitySearchResult]:
        """向量 kNN 检索实体记录。"""
