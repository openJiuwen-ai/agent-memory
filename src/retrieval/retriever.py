"""Retriever — 检索层入口（编排架构 §7 的完整链路）。

单次 :meth:`retrieve` 驱动：查询理解 → 并行多路召回（按配置启用的
通道）→ 融合 + 重排 → 渐进式披露 → 返回结果与检索轨迹。
记忆接口层的 ``recall`` 映射到本接口；QueryParser / Recaller / Fuser /
Discloser 由装配注入。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import Scope

from .base import RetrievalOperator
from .types import RetrievalQuery, RetrievalResult


class Retriever(RetrievalOperator):
    @abstractmethod
    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        """在 ``scope`` 范围内执行完整检索链路，返回结果项与（按需的）检索
        轨迹。scope 作为显式首参贯穿全链路并下推到各 Store 做隔离，不随
        ``query`` 携带。"""
