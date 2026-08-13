"""Tokenizer — 分词能力。

**共用说明**：构建层建全文倒排索引时对文档分词；检索层做关键词检索时
对 query 分词。两侧必须使用同一分词器，否则 term 对不上、倒排召回错位。
"""

from __future__ import annotations

from abc import abstractmethod

from ..factory.factory import Factory
from ..base import Plugin


class TokenizerProducer(Factory):
    """Tokenizer 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``tokenizer_impl`` 下以 ``@TokenizerProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "tokenizer"


class Tokenizer(Plugin):
    @abstractmethod
    def tokenize(self, text: str) -> list[str]:
        """将 ``text`` 切分为 token 序列。"""

    def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
        """批量分词；默认逐条调用 :meth:`tokenize`，后端可覆写提速。"""
        return [self.tokenize(t) for t in texts]
