# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""治理查询端口：封装 ``Governor`` 的 inspect/trace/audit。"""

from __future__ import annotations

from jiuwen_memory.common.type_def import AuditEvent, MemoryUnit, Scope
from jiuwen_memory.control.governance import Governor


class GovernanceService:
    """已鉴权治理读取入口。编辑/遗忘仍走数据面命令端口。"""

    def __init__(self, governor: Governor) -> None:
        self._governor = governor

    def inspect(self, unit_ids: list[str], scope: Scope) -> list[MemoryUnit]:
        return self._governor.inspect(unit_ids, scope)

    def trace(self, unit_id: str, scope: Scope) -> list[MemoryUnit]:
        return self._governor.trace(unit_id, scope)

    def audit(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        return self._governor.audit(filters, limit)
