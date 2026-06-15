"""Classifier — 多维分类（架构 §6.1）。

按主题/认知角色/来源/重要度等多维度归类：认知角色（tier）决定记忆
常驻上下文还是按需检索；标签写入 ``tags``/``metadata`` 供检索前置过滤。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import MemoryUnit

from .base import ConstructionOperator


class Classifier(ConstructionOperator):
    @abstractmethod
    def classify(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """为一批记忆单元打上 tier/主题/重要度等分类标签，返回更新后的单元。"""
