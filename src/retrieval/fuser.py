"""Fuser — 多路融合 + 重排（架构 §7 ③）。

把各召回通道的候选合并去重、归一化打分并融合排序（如 RRF / 加权），
可选调用共享的 :class:`~common.reranker.Reranker` 做精排。
重排开关与融合策略按配置裁剪（端侧可关重排降时延）。
"""

from __future__ import annotations

from abc import abstractmethod

from .base import RetrievalOperator
from .types import ParsedQuery, ScoredUnit


class Fuser(RetrievalOperator):
    @abstractmethod
    def fuse(self, query: ParsedQuery, candidates: list[list[ScoredUnit]]) -> list[ScoredUnit]:
        """融合多路候选（每路一个列表），返回统一排序后的 top 候选。"""
