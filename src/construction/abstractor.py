"""Abstractor — 抽象与精炼/升华（架构 §6.1）。

在低/中抽象记忆之上做概括：情景→语义、经验→技能/模式，升华出画像、
长期偏好、可复用技能等**高抽象粒度**记忆。产物通过 ``provenance``
记录血缘（由哪些 unit 升华而来），保证可重建、可审计回溯。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import MemoryUnit

from .base import ConstructionOperator


class Abstractor(ConstructionOperator):
    @abstractmethod
    def abstract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """对一批记忆单元做抽象与精炼，产出高抽象粒度的新记忆单元。"""
