"""Consolidator — 候选记忆落盘前的隐式巩固。"""

from __future__ import annotations

from abc import abstractmethod

from common.factory.factory import Factory
from common.type_def import MemoryUnit

from .base import ConstructionOperator
from .evolver import EvolveResult


class ConsolidatorProducer(Factory):
    """Consolidator 注册式工厂。"""

    TOP_NAME = "consolidator"


class Consolidator(ConstructionOperator):
    """对候选执行召回、判定及 ADD/UPDATE/SUPERSEDE/NOOP 落盘动作。"""

    @abstractmethod
    def consolidate(self, candidates: list[MemoryUnit]) -> EvolveResult:
        """巩固候选并返回本次真源变更。"""
