"""最小实现：:class:`~control.governance.Governor`。

治理「看」侧：按目标 scope 检视 / 沿 ``provenance`` 来源链回溯 / 审计过滤查询。
真源读注入的 :class:`~storage.kv.KVStore`，审计读注入的事件列表。
"""

from __future__ import annotations

from common.audit.base import AuditLogger, AuditProducer
from common.type_def import AuditEvent, MemoryUnit, Scope
from control.base import ControlOperatorType
from control.governance import Governor, GovernorProducer
from storage.storage import Storage, StorageProducer


class InMemoryGovernor(Governor):
    """治理「看」侧：按目标 scope 检视 / 沿 provenance 回溯 / 审计过滤查询。"""

    def __init__(self, storage: Storage, audit_logger: AuditLogger) -> None:
        self._storage = storage
        self._audit = audit_logger

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.GOVERNOR

    def health(self) -> None:
        return None

    def _find(self, unit_id: str, scope: Scope) -> MemoryUnit | None:
        units = self._storage.get(scope, [unit_id])
        return units[0] if units else None

    def inspect(self, unit_ids: list[str], scope: Scope) -> list[MemoryUnit]:
        found = [self._find(uid, scope) for uid in unit_ids]
        return [u for u in found if u is not None]

    def trace(self, unit_id: str, scope: Scope) -> list[MemoryUnit]:
        chain: list[MemoryUnit] = []
        seen: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in seen:
                return
            cursor = self._find(current_id, scope)
            if cursor is None:
                return
            seen.add(current_id)
            chain.append(cursor)
            for parent_id in cursor.provenance:
                visit(parent_id)

        visit(unit_id)
        return chain

    def audit(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        return self._audit.query(filters, limit)


# -- 注册到 GovernorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@GovernorProducer.register("in_memory")
def _build(config):
    # 与对外 API 注入的 audit_logger 共享同一实例（缓存键 "audit"）→ 治理读到同一审计事件流。
    audit = AuditProducer.dep(config, default="sqlite")
    return InMemoryGovernor(StorageProducer.resolve(config), audit)
