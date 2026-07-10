from __future__ import annotations

from api import Scope
from api.memory_api_impl import build_kernel
from common.audit.base import AuditLogger
from common.type_def import AuditEvent, MemoryUnit, Segment, memory_key
from common.type_def.memory_codec import dumps
from control.governance_impl.in_memory_governor import InMemoryGovernor


class _QueryOnlyAuditLogger(AuditLogger):
    def __init__(self, events: list[AuditEvent]) -> None:
        self._events = events

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        return self._events[:limit]


def test_trace_follows_provenance_sources_depth_first() -> None:
    scope = Scope(user="u1")
    kernel = build_kernel()
    source = MemoryUnit(id="source", scope=scope, segments=[Segment(content="source")])
    direct = MemoryUnit(
        id="direct",
        scope=scope,
        segments=[Segment(content="direct")],
        provenance=["source"],
    )
    nested = MemoryUnit(
        id="nested",
        scope=scope,
        segments=[Segment(content="nested")],
        provenance=["direct"],
    )
    for unit in [source, direct, nested]:
        kernel.kv.insert(scope, memory_key(unit.id), dumps(unit))

    assert [unit.id for unit in kernel.api.trace("nested", scope, identity=scope)] == [
        "nested",
        "direct",
        "source",
    ]


def test_trace_stops_on_provenance_cycles() -> None:
    scope = Scope(user="u1")
    kernel = build_kernel()
    a = MemoryUnit(id="a", scope=scope, segments=[Segment(content="a")], provenance=["b"])
    b = MemoryUnit(id="b", scope=scope, segments=[Segment(content="b")], provenance=["a"])
    for unit in [a, b]:
        kernel.kv.insert(scope, memory_key(unit.id), dumps(unit))

    assert [unit.id for unit in kernel.api.trace("a", scope, identity=scope)] == ["a", "b"]


def test_audit_uses_logger_query_interface() -> None:
    kernel = build_kernel()
    event = AuditEvent(action="write", layer="api")
    governor = InMemoryGovernor(kernel.kv, _QueryOnlyAuditLogger([event]))

    assert governor.audit({"action": "write"}, limit=10) == [event]
