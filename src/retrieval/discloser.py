"""Discloser — 渐进式披露（架构 §7 ④）。

按需加载、控制 token：L0 只给摘要，L1 给相关片段，L2 给全文。
调用方先拿 L0 浏览，需要细节再对单条升级到 L1/L2，避免一次性
塞入全部原文。
"""

from __future__ import annotations

from abc import abstractmethod

from .base import RetrievalOperator
from .types import DisclosureLevel, ParsedQuery, RetrievedItem, ScoredUnit


class Discloser(RetrievalOperator):
    @abstractmethod
    def disclose(
        self, query: ParsedQuery, candidates: list[ScoredUnit], level: DisclosureLevel
    ) -> list[RetrievedItem]:
        """按披露层级为候选加载内容（L0 摘要 / L1 片段 / L2 全文）。
        ``query`` 提供改写后查询与关键词，L1 据此从全文中挑选与查询最相关
        的片段（L0/L2 不依赖 query，但保持签名一致）。"""
