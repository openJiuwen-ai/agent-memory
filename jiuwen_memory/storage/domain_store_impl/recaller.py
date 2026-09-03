# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recaller — 单路召回，数据面 :class:`CompositeDomainStore` 的检索适配器。

一个 Recaller 对应一条召回通道（向量 ANN / 关键词 BM25 / 图遍历 /
文档定位 / 时序过滤），消费 :class:`ParsedQuery` 中本通道需要的字段，
经 ``StoreManager`` 的对应命名端口召回候选。scope/标签前置过滤下推到通道内执行。
多路并行与结果合并由 ``CompositeDomainStore.recall`` 负责，通道之间互不感知。

**归属**：Recaller 是数据面的内部件而非检索层算子——生产链路里它的唯一消费方是
``CompositeDomainStore``（由 ``_assemble_recallers`` 在 manager 装配期组装并
``bind_recallers`` 注入），``PipelineRetriever`` 只按首选路径委托
``domain_store.recall``/``recall_and_get``/``retrieve``，不持有召回路。故契约与实现
都落在 ``storage/domain_store_impl``，不再继承检索层的 ``RetrievalOperator``：
自描述的 ``operator_type()`` 对一个不进检索算子表的组件没有意义。``health()`` 保留
——它是 manager 健康聚合与手工探活的实际入口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ParsedQuery, RecallChannel, Scope, ScoredUnit


class RecallerProducer(Factory):
    """Recaller 的注册式工厂（与契约同处一处，消费方只依赖契约即可取实例）。

    ``name`` 即实现名（如 keyword / vector / graph）。各实现在同目录下以
    ``@RecallerProducer.register("<名>")`` 自注册——由
    :func:`storage.bootstrap.register_backends` import ``domain_store_impl`` 时统一触发。
    ``TOP_NAME`` 仍是 ``recaller``：YAML 命名空间与注册名与迁移前逐字一致。
    """

    TOP_NAME = "recaller"


class Recaller(ABC):
    @abstractmethod
    def channel(self) -> RecallChannel:
        """返回本召回路对应的通道。"""

    @abstractmethod
    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        """在 ``scope`` 范围内、本通道内召回 top-k 候选记忆单元：本算子据此
        **组装**底层 Store 的检索查询——``scope`` 落到查询的专用 ``scope`` 字段
        做原生隔离，``query.scalar_filters`` 落到查询的 ``filters`` 做元数据硬
        过滤；不把 scope 混进 filters 透传。
        """

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""
