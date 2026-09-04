# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Chunker — 内容切分能力。

**共用说明**：构建层写入时把内容切成 :class:`Chunk`（向量化与建索引的
基本单元）；重索引与自演进路径必须按同一规则重切，保证派生索引可重建、
切分结果可复现。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from ..base import Plugin
from ..factory.factory import Factory
from ..type_def import Chunk


class ChunkerProducer(Factory):
    """Chunker 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``chunker_impl`` 下以 ``@ChunkerProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "chunker"


class Chunker(Plugin):
    @abstractmethod
    def chunk(
        self,
        text: str,
        unit_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """将 ``text`` 切分为有序 chunk，每块带上 ``unit_id`` 与透传的 ``metadata``。"""
