# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Discloser — 渐进式披露（架构 §7 ④）。

按需加载、控制 token：L0 只给摘要，L1 给相关片段，L2 给全文。
调用方先拿 L0 浏览，需要细节再对单条升级到 L1/L2，避免一次性
塞入全部原文。

纯内容塑形：候选已经过物化、有效性过滤及评分，必须按候选顺序一对一输出，不能
再点读、过滤或重排。ScoredMemoryUnit 优先使用其自身 unit 保留完整 Scope 身份；
units 裸 id 表只兼容 ScoredUnit。query 提供关键词，max_tokens 供自适应披露估算。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, ScoredCandidate, ScoredMemoryUnit

from .base import RetrievalOperator
from .types import DisclosureLevel, ParsedQuery, RetrievedItem


def candidate_unit(
    candidate: ScoredCandidate, units: dict[str, MemoryUnit],
) -> MemoryUnit | None:
    """物化候选携带完整 Scope 身份；只有旧裸 id 候选才回退查找表。"""
    if isinstance(candidate, ScoredMemoryUnit):
        return candidate.unit
    return units.get(candidate.unit_id)


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
        """按候选顺序及披露层级塑形 L0 摘要、L1 片段与 L2 全文。"""
