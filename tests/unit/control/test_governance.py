from __future__ import annotations

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.type_def import AuditEvent, MemoryUnit, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config.config import Config
from jiuwen_memory.control.governance_impl.in_memory_governor import InMemoryGovernor
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage

pytestmark = pytest.mark.unit

_TEST_KEY_HEX = "00" * 32


def _test_kernel():
    config = Config.from_dict(
        {
            "security": {
                "default": {"target": "local", "params": {"key_hex": _TEST_KEY_HEX}}
            }
        }
    )
    return build_kernel(kv=InMemoryKVStore(), config=config)


class _QueryOnlyAuditLogger(AuditLogger):
    def __init__(self, events: list[AuditEvent]) -> None:
        self._events = events

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        return self._events[:limit]


class _FailingKV:
    @staticmethod
    def get(scope: Scope, key: str) -> bytes:
        raise BackendError("storage unavailable")

    @staticmethod
    def mget(scope: Scope, keys: list[str]) -> list[bytes]:
        raise BackendError("storage unavailable")


def test_trace_follows_provenance_sources_depth_first() -> None:
    scope = Scope(user="u1")
    kernel = _test_kernel()
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
    kernel = _test_kernel()
    a = MemoryUnit(id="a", scope=scope, segments=[Segment(content="a")], provenance=["b"])
    b = MemoryUnit(id="b", scope=scope, segments=[Segment(content="b")], provenance=["a"])
    for unit in [a, b]:
        kernel.kv.insert(scope, memory_key(unit.id), dumps(unit))

    assert [unit.id for unit in kernel.api.trace("a", scope, identity=scope)] == ["a", "b"]


def test_inspect_is_bound_to_the_authorized_scope() -> None:
    kernel = _test_kernel()
    scope_a = Scope(org="acme", space="space-a", user="alice")
    scope_b = Scope(org="acme", space="space-b", user="alice")
    unit_a = MemoryUnit(
        id="shared-id",
        scope=scope_a,
        segments=[Segment(content="space A secret")],
    )
    unit_b = MemoryUnit(
        id="shared-id",
        scope=scope_b,
        segments=[Segment(content="space B content")],
    )
    kernel.kv.insert(scope_a, memory_key(unit_a.id), dumps(unit_a))
    kernel.kv.insert(scope_b, memory_key(unit_b.id), dumps(unit_b))

    inspected = kernel.api.inspect([unit_b.id], scope_b, identity=scope_b)

    assert [unit.content for unit in inspected] == ["space B content"]


def test_inspect_does_not_hide_storage_failures() -> None:
    governor = InMemoryGovernor(CompositeStorage(kv=_FailingKV()), _QueryOnlyAuditLogger([]))

    with pytest.raises(BackendError, match="storage unavailable"):
        governor.inspect(["unit-id"], Scope(org="acme"))


def test_audit_uses_logger_query_interface() -> None:
    kernel = _test_kernel()
    event = AuditEvent(action="write", layer="api")
    governor = InMemoryGovernor(kernel.storage, _QueryOnlyAuditLogger([event]))

    assert governor.audit({"action": "write"}, limit=10) == [event]
