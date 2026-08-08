"""Fuser — 多路融合 + 重排（架构 §7 ③）。

把各召回通道的候选合并去重、归一化打分并融合排序（如 RRF / 加权），
可选调用共享的 :class:`~common.reranker.Reranker` 做精排。
重排开关与融合策略按配置裁剪（端侧可关重排降时延）。
"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import ScoredCandidate

from .base import RetrievalOperator
from .types import ParsedQuery


class FuserProducer(Factory):
    """Fuser 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``fuser_impl`` 下以 ``@FuserProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由
    :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "fuser"


class Fuser(RetrievalOperator):
    @abstractmethod
    def fuse(
        self, query: ParsedQuery, candidates: list[list[ScoredCandidate]]
    ) -> list[ScoredCandidate]:
        """融合多路候选（每路一个列表），返回统一排序后的 top 候选。"""
