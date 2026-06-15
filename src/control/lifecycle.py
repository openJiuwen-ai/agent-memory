"""LifecycleManager — 生命周期管理（架构 §3.1）。

管理记忆单元的状态流转：active → superseded（被取代）/ archived（归档）
/ forgotten（遗忘）。一律「标记失效」而非物理删除（非破坏式，保留
provenance 血缘），合规删除等硬删除场景由治理流程显式触发。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import LifecycleState

from .base import ControlOperator


class LifecycleManager(ControlOperator):
    @abstractmethod
    def transition(self, unit_ids: list[str], target: LifecycleState) -> None:
        """将一批记忆单元流转到目标状态（非破坏式标记）。"""

    @abstractmethod
    def sweep(self) -> list[str]:
        """扫描到期（t_invalid 已过）/低价值的记忆，按策略降权、归档或
        遗忘；返回被处理的记忆单元 id。"""
