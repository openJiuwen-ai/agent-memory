# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reranker — 重排能力：对候选记忆按与 query 的相关性精排。

**共用说明**：检索层在多路召回融合后调用它做精排（粗排靠各索引自身
得分，精排靠交叉编码等更强模型）；构建层/自演进的写入流水线做
「抽取 → 相似检索 → 去重/冲突消解决策」时，同样需要对召回的候选
记忆按相关性排序。重排器是可插拔组件（架构 §12），端侧可降级关闭。

除打分外，:class:`Reranker` 还提供两个非抽象默认入口：

- ``rerank_texts``：无完整 ``MemoryUnit`` 上下文的路径（如 schema_temporal
  的 Property 精排）用字符串入参的兼容入口，内部包成空 ``MemoryUnit`` 再调
  ``rerank``。
- ``resolve``：可选语义去重 + 冲突消解。默认识别不发候选；仅 LLM 档
  覆写。任何失败由调用方降级，实现内部不吞异常。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from ..base import Plugin
from ..factory.factory import Factory
from ..type_def import MemoryUnit, Segment


class RerankerProducer(Factory):
    """Reranker 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。
    各实现在 ``reranker_impl`` 下以 ``@RerankerProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`common.bootstrap.register_plugins` 统一触发。
    """

    TOP_NAME = "reranker"


@dataclass
class DuplicateGroup:
    """重复分组：簇内代表 + 被去重的条目（供轨迹统计用）。

    ``representative`` 与 ``dropped`` 均为**输入下标**，指向真源 MemoryUnit。
    """

    representative: int = -1  # 保留的代表下标（输入下标）
    dropped: list[int] = field(default_factory=list)  # 被去重的下标


@dataclass
class RerankResolution:
    """去重 + 冲突消解的结果。

    ``kept_indices`` 是输入下标的**稳定子序列**——不合成新内容，每个保留下标仍
    指向真源 MemoryUnit，``unit_id`` ↔ 真源对应不断裂。平局兜底时 ``kept_indices``
    包含双方——不悄悄裁决，把冲突暴露在轨迹里。``duplicates`` 与 ``conflict_dropped``
    仅供轨迹 detail 计数，不向终端调用方透出结构化冲突标注。
    """

    kept_indices: list[int] = field(default_factory=list)  # 保留的输入下标（有序，子序列）
    duplicates: list[DuplicateGroup] = field(default_factory=list)  # 重复分组（轨迹统计用）
    conflict_dropped: list[int] = field(default_factory=list)  # 冲突中被丢弃的下标

    @classmethod
    def keep_all(cls, n: int) -> RerankResolution:
        """构建「全部保留」的默认结果（非 LLM 实现的 resolve 默认行为）。"""
        return cls(kept_indices=list(range(n)), duplicates=[], conflict_dropped=[])


class Reranker(Plugin):
    @abstractmethod
    def rerank(self, query: str, units: list[MemoryUnit]) -> list[float]:
        """对每条 ``MemoryUnit`` 计算与 ``query`` 的相关性得分（顺序与输入一致，
        分值越大越相关）；排序/截断由调用方完成。实现可消费 ``unit.content /
        tier / temporal / tags / entities / provenance`` 等字段作为辅助判据。
        """

    def rerank_texts(self, query: str, texts: list[str]) -> list[float]:
        """``str`` 入参的兼容入口（schema_temporal 等无完整 unit 的路径用）。

        默认把每个 text 包成 ``MemoryUnit(segments=[Segment(content=t)])`` 再调
        :meth:`rerank`。
        """
        units = [MemoryUnit(segments=[Segment(content=t)]) for t in texts]
        return self.rerank(query, units)

    def resolve(self, query: str, units: list[MemoryUnit]) -> RerankResolution:
        """可选：语义去重 + 冲突消解。默认 ``keep_all``（不丢任何候选）。

        仅 LLM 档覆写。调用方统一调 ``resolve``，不需要 ``hasattr`` 判断；任何
        异常由调用方降级，实现内部不吞。
        """
        return RerankResolution.keep_all(len(units))
