# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Embedder — 向量化能力：文本 -> 稠密向量。

**共用说明**：构建层写入时对 chunk 向量化建向量索引；检索层读取时对
query 向量化做 ANN 召回。两侧必须使用同一 embedder（同模型、同维度），
才能落在同一向量空间。
"""

from __future__ import annotations

from abc import abstractmethod

from ..factory.factory import Factory
from ..base import Plugin


class EmbedderProducer(Factory):
    """Embedder 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``embedder_impl`` 下以 ``@EmbedderProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "embedder"


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
