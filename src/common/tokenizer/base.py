"""Tokenizer — 分词能力。

**共用说明**：构建层建全文倒排索引时对文档分词；检索层做关键词检索时
对 query 分词。两侧必须使用同一分词器，否则 term 对不上、倒排召回错位。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin


class Tokenizer(Plugin):
    @abstractmethod
    def tokenize(self, text: str) -> list[str]:
        """将 ``text`` 切分为 token 序列。"""

    def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
        """批量分词；默认逐条调用 :meth:`tokenize`，后端可覆写提速。"""
        return [self.tokenize(t) for t in texts]
