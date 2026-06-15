"""Embedder — 向量化能力：文本 -> 稠密向量。

**共用说明**：构建层写入时对 chunk 向量化建向量索引；检索层读取时对
query 向量化做 ANN 召回。两侧必须使用同一 embedder（同模型、同维度），
才能落在同一向量空间。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin


class Embedder(Plugin):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：每条输入产出一个向量，顺序与输入一致。"""

    @abstractmethod
    def dimension(self) -> int:
        """返回输出向量维度（须与目标向量索引一致）。"""

    def embed_query(self, text: str) -> list[float]:
        """单条便捷方法；默认包装 :meth:`embed`。"""
        return self.embed([text])[0]
