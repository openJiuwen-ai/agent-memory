"""Reranker — 重排能力：对候选文本按与 query 的相关性精排。

**共用说明**：检索层在多路召回融合后调用它做精排（粗排靠各索引自身
得分，精排靠交叉编码等更强模型）；构建层/自演进的写入流水线做
「抽取 → 相似检索 → 去重/冲突消解决策」时，同样需要对召回的候选
记忆按相关性排序。重排器是可插拔组件（架构 §12），端侧可降级关闭。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin


class Reranker(Plugin):
    @abstractmethod
    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """对一批候选文本计算与 ``query`` 的相关性得分（顺序与输入一致，
        分值越大越相关）；排序/截断由调用方完成。"""
