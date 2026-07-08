"""最小实现：:class:`~control.governance.Governor`。

治理「看」侧：跨 scope 检视 / 沿 ``provenance`` 来源链回溯 / 审计过滤查询。
真源读注入的 :class:`~storage.kv.KVStore`（``scopes()`` + ``list()`` 做跨 scope 枚举，
字节经 :func:`~common.type_def.memory_codec.loads` 在产出结果时反序列化），审计读注入的
事件列表（与 :class:`~common.audit.audit_impl.in_memory_audit_logger.InMemoryAuditLogger`
共享同一 list）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from common.audit.base import AuditProducer
from common.type_def import AuditEvent, MemoryUnit, memory_key
from common.type_def.memory_codec import loads
from control.base import ControlOperatorType
from control.governance import Governor, GovernorProducer
from storage.kv import KvProducer, KVStore


class InMemoryGovernor(Governor):
    """治理「看」侧：跨 scope 检视 / 沿 provenance 回溯 / 审计过滤查询。"""

    def __init__(self, kv: KVStore, audit_events: List[AuditEvent]) -> None:
        self._kv = kv
        self._audit = audit_events

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.GOVERNOR

    def health(self) -> None:
        return None

    def _find(self, unit_id: str) -> Optional[MemoryUnit]:
        """按 unit_id 直接查 KVStore——治理可跨 scope 检视，逐 scope 尝试 get。"""
        for scope in self._kv.scopes():
            try:
                raw = self._kv.get(scope, memory_key(unit_id))
                unit = loads(raw)
                if unit is not None:
                    return unit
            except Exception:
                continue  # 该 scope 下不存在此 key，跳过
        return None

    def inspect(self, unit_ids: List[str]) -> List[MemoryUnit]:
        found = [self._find(uid) for uid in unit_ids]
        return [u for u in found if u is not None]

    def trace(self, unit_id: str) -> List[MemoryUnit]:
        chain: List[MemoryUnit] = []
        seen: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in seen:
                return
            cursor = self._find(current_id)
            if cursor is None:
                return
            seen.add(current_id)
            chain.append(cursor)
            for parent_id in cursor.provenance:
                visit(parent_id)

        visit(unit_id)
        return chain

    def audit(self, filters: Dict[str, str], limit: int = 100) -> List[AuditEvent]:
        out: List[AuditEvent] = []
        for ev in self._audit:
            if filters.get("action") and ev.action != filters["action"]:
                continue
            if filters.get("layer") and ev.layer != filters["layer"]:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out


# -- 注册到 GovernorProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@GovernorProducer.register("in_memory")
def _build(config):
    # 与对外 API 注入的 audit_logger 共享同一实例（缓存键 "audit"）→ 治理读到同一审计事件流。
    audit = AuditProducer.dep(config, default="in_memory")
    return InMemoryGovernor(KvProducer.dep(config, default="memory"), audit.events)
