"""Recaller — 单路召回。

一个 Recaller 对应一条召回通道（向量 ANN / 关键词 BM25 / 图遍历 /
文档定位 / 时序过滤），消费 :class:`ParsedQuery` 中本通道需要的字段，
经注入的对应 Store 召回候选。scope/标签前置过滤下推到通道内执行。
多路并行与结果合并由 Retriever/Fuser 负责，通道之间互不感知。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import Scope

from .base import RetrievalOperator
from .types import ParsedQuery, RecallChannel, ScoredUnit


class RecallerProducer(Factory):
    """Recaller 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名（如 keyword / vector / graph）。各实现在 ``recaller_impl`` 下以
    ``@RecallerProducer.register("<名>")`` 自注册——由 :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "recaller"


class Recaller(RetrievalOperator):
    @abstractmethod
    def channel(self) -> RecallChannel:
        """返回本召回路对应的通道。"""

    @abstractmethod
    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        """在 ``scope`` 范围内、本通道内召回 top-k 候选记忆单元：本算子据此
        **组装**底层 Store 的检索查询——``scope`` 落到查询的专用 ``scope`` 字段
        做原生隔离，``query.scalar_filters`` 落到查询的 ``filters`` 做元数据硬
        过滤；不把 scope 混进 filters 透传。"""
