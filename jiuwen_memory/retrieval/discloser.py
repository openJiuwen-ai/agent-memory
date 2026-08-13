"""Discloser — 渐进式披露（架构 §7 ④）。

按需加载、控制 token：L0 只给摘要，L1 给相关片段，L2 给全文。
调用方先拿 L0 浏览，需要细节再对单条升级到 L1/L2，避免一次性
塞入全部原文。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, ScoredCandidate

from .base import RetrievalOperator
from .types import DisclosureLevel, ParsedQuery, RetrievedItem


class DiscloserProducer(Factory):
    """Discloser 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``discloser_impl`` 下以 ``@DiscloserProducer.register("<名>")``
    自注册——注册发生在 import 实现模块时，由
    :func:`retrieval.bootstrap.register_operators` 统一触发。
    """

    TOP_NAME = "discloser"


class Discloser(RetrievalOperator):
    @abstractmethod
    def disclose(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        units: dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> list[RetrievedItem]:
        """按披露层级为候选**塑形内容**（L0 摘要 / L1 片段 / L2 全文）。

        纯内容塑形：候选记忆单元已由编排者（Retriever）经 UnitReader 点读、
        有效性过滤、（可选）重排后给定——``candidates`` 是最终顺序的
        ``ScoredCandidate`` 列表，``units`` 是 ``unit_id → MemoryUnit`` 的内容查找表。
        本算子**不**再做点读 / 过滤 / 重排，只按 ``level`` 截/取内容产出结果。
        ``query`` 提供改写后查询与关键词，L1 据此从全文挑与查询最相关的片段。
        ``max_tokens`` 用于自适应披露预算估算；非自适应模式可忽略。
        """
