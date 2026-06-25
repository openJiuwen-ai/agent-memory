from __future__ import annotations

from api import Scope
from api.memory_api_impl import build_kernel
from common.type_def import MemoryUnit, Segment
from common.type_def.memory_codec import dumps


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
        kernel.kv.insert(scope, unit.id, dumps(unit))

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
        kernel.kv.insert(scope, unit.id, dumps(unit))

    assert [unit.id for unit in kernel.api.trace("a", scope, identity=scope)] == ["a", "b"]
