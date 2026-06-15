"""Chunker — 内容切分能力。

**共用说明**：构建层写入时把内容切成 :class:`Chunk`（向量化与建索引的
基本单元）；重索引与自演进路径必须按同一规则重切，保证派生索引可重建、
切分结果可复现。
"""

from __future__ import annotations

from abc import abstractmethod

from ..base import Plugin
from ..type_def import Chunk


class Chunker(Plugin):
    @abstractmethod
    def chunk(
        self,
        text: str,
        unit_id: str = "",
        metadata: dict[str, str] | None = None,
    ) -> list[Chunk]:
        """将 ``text`` 切分为有序 chunk，每块带上 ``unit_id`` 与透传的 ``metadata``。"""
