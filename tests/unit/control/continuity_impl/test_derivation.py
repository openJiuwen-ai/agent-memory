"""Derivation recovery: frozen plan, output keys, per-item reconciliation."""

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import MemoryRef
from jiuwen_memory.control.continuity_impl.derivation import (
    DerivationItem,
    DerivationPlan,
    reconcile,
)

pytestmark = pytest.mark.unit

SCOPE = Scope(org="o", space="s")


def _unit(unit_id, content, *, output_key=None):
    return MemoryUnit(
        id=unit_id,
        scope=SCOPE,
        segments=[Segment(content=content)],
        system_metadata={"continuity_output": output_key} if output_key else {},
    )


def _digest(content):
    unit = MemoryUnit(id="x", scope=SCOPE, segments=[Segment(content=content)])
    return MemoryRef.of(unit).content_digest


class Chain:
    def __init__(self):
        self.units: dict[str, MemoryUnit] = {}
        self.sources: set[tuple[str, str]] = set()
        self.created: list[str] = []

    def query_output(self, output_key):
        return [
            u for u in self.units.values()
            if u.system_metadata.get("continuity_output") == output_key
        ]

    def read(self, ref):
        return self.units[ref.unit_id]

    def create(self, item):
        unit = _unit(item.output_key, item.content, output_key=item.output_key)
        self.units[unit.id] = unit
        self.created.append(unit.id)
        return unit

    def add_source(self, event_key, unit_id):
        self.sources.add((event_key, unit_id))


def _run(chain, plan):
    return reconcile(
        plan,
        query_output=chain.query_output,
        read=chain.read,
        create=chain.create,
        add_source=chain.add_source,
    )


def test_created_output_is_adopted_when_already_written():
    chain = Chain()
    existing = _unit("out-0", "derived", output_key="out-0")
    chain.units[existing.id] = existing
    item = DerivationItem(
        output_key="out-0", action="created", content="derived",
        content_digest=MemoryRef.of(existing).content_digest,
    )
    result = _run(chain, DerivationPlan("b", [item]))
    assert result.status == "committed"
    assert result.derived[0]["action"] == "created"
    assert result.derived[0]["ref"]["unit_id"] == "out-0"
    assert chain.created == []  # adopted, never duplicated


def test_created_output_digest_mismatch_conflicts_without_overwriting():
    chain = Chain()
    existing = _unit("out-0", "original", output_key="out-0")
    chain.units[existing.id] = existing
    item = DerivationItem(
        output_key="out-0", action="created", content="different", content_digest="0" * 64
    )
    result = _run(chain, DerivationPlan("b", [item]))
    assert result.status == "partial"
    assert result.conflicts == ["out-0"]
    assert chain.units["out-0"].segments[0].content == "original"


def test_reused_source_association_is_set_semantics():
    chain = Chain()
    target = _unit("t", "reused memory")
    chain.units["t"] = target
    item = DerivationItem(
        output_key="reuse-0", action="reused", ref=MemoryRef.of(target), event_key="evt"
    )
    plan = DerivationPlan("b", [item])
    assert _run(chain, plan).status == "committed"
    assert _run(chain, plan).status == "committed"  # replay
    assert chain.sources == {("evt", "t")}  # no duplicate association


def test_reused_unreadable_target_is_partial():
    chain = Chain()
    ghost = MemoryRef(SCOPE, "missing", "a" * 64)
    item = DerivationItem(output_key="reuse-0", action="reused", ref=ghost, event_key="evt")
    result = _run(chain, DerivationPlan("b", [item]))
    assert result.status == "partial"
    assert result.conflicts == ["reuse-0"]


def test_missing_output_creates_and_derived_matches_plan():
    chain = Chain()
    item = DerivationItem(
        output_key="out-0", action="created", content="new", content_digest=_digest("new")
    )
    result = _run(chain, DerivationPlan("b", [item]))
    assert result.status == "committed"
    assert result.derived[0]["action"] == "created"
    assert result.derived[0]["ref"]["unit_id"] == "out-0"


def test_missing_query_output_fails_closed_for_created():
    chain = Chain()
    item = DerivationItem(
        output_key="out-0", action="created", content="new", content_digest=_digest("new")
    )
    result = reconcile(
        DerivationPlan("b", [item]),
        query_output=None,
        read=chain.read,
        create=chain.create,
        add_source=chain.add_source,
    )
    assert result.status == "partial"
    assert result.conflicts == ["out-0"]
    assert chain.created == []  # never created without a reliable adoption check


def test_duplicate_output_identity_is_not_arbitrarily_adopted():
    chain = Chain()
    for key in ("one", "two"):
        chain.units[key] = _unit(key, "same", output_key="output")
    item = DerivationItem("output", "created", content="same", content_digest=_digest("same"))
    assert _run(chain, DerivationPlan("b", [item])).status == "partial"
    assert chain.created == []


def test_reused_target_modified_after_plan_is_not_adopted():
    chain = Chain()
    target = _unit("t", "before")
    item = DerivationItem("reuse", "reused", ref=MemoryRef.of(target), event_key="event")
    chain.units["t"] = _unit("t", "after")
    result = _run(chain, DerivationPlan("batch", [item]))
    assert result.status == "partial"
    assert result.derived == []
    assert chain.sources == set()
