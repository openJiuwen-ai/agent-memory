"""Governor — 治理（架构 §12：可检视/可回溯/可审计）。

透明可治理的落点：记忆可被人检视、沿 provenance 血缘回溯「这条记忆
从哪来」、按条件查询审计留痕。编辑/遗忘动作本身走记忆接口层的
update/delete，本算子负责「看」的一侧。
"""

from __future__ import annotations

from abc import abstractmethod

from common.type_def import AuditEvent, MemoryUnit

from .base import ControlOperator


class Governor(ControlOperator):
    @abstractmethod
    def inspect(self, unit_ids: list[str]) -> list[MemoryUnit]:
        """检视：读取记忆单元的完整内容与治理字段（含已失效版本）。"""

    @abstractmethod
    def trace(self, unit_id: str) -> list[MemoryUnit]:
        """回溯：沿 provenance 血缘向上追溯，返回该记忆的演进来源链。"""

    @abstractmethod
    def audit(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        """审计查询：按条件（actor/action/layer/时间段等）检索审计事件。"""
