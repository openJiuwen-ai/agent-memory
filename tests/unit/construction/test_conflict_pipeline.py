"""Conflict consolidation pipeline: injected resolver, candidate status preserved."""

from dataclasses import asdict
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import Relation, Scope
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, RelationStatus
from jiuwen_memory.construction.conflict_resolver import ConflictResolution as D
from jiuwen_memory.construction.conflict_resolver import Resolution
from jiuwen_memory.construction.conflict_resolver_impl.pipeline import resolve_discovered_conflicts

pytestmark = pytest.mark.unit

SCOPE = Scope(org="o", space="s")
DIGEST = "a" * 64


def _ref(unit_id: str) -> MemoryRef:
    return MemoryRef(SCOPE, unit_id, DIGEST)


def _relation(source: str, target: str, kind: str) -> Relation:
    return Relation(source_id=source, target_id=target, relation=kind, score=0.8, metadata={})


def test_resolver_is_called_and_candidate_conflict_stays_candidate():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(D.DEFER, "insufficient evidence")
    records = resolve_discovered_conflicts(
        [_relation("a", "b", "contradicts")],
        {"a": _ref("a"), "b": _ref("b")},
        resolver,
        supports=lambda key: [],
        read=lambda ref: Mock(content=ref.unit_id),
    )
    resolver.resolve.assert_called_once()
    assert records[0].status == RelationStatus.CANDIDATE
    assert records[0].resolution == "defer"
    assert records[0].resolution_origin == "automatic"


def test_resolved_recommendation_never_auto_confirms():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(D.PREFER_B, "authorized positive support")
    records = resolve_discovered_conflicts(
        [_relation("a", "b", "contradicts")],
        {"a": _ref("a"), "b": _ref("b")},
        resolver,
        supports=lambda key: [],
        read=lambda ref: Mock(content=ref.unit_id),
    )
    assert records[0].resolution == "prefer_b"
    # A resolved recommendation is attached, but the relation remains candidate:
    # auto-confirmation requires an explicit, evidence-backed review.
    assert records[0].status == RelationStatus.CANDIDATE


def test_caused_by_maps_to_possible_cause_of_as_candidate():
    resolver = Mock()
    records = resolve_discovered_conflicts(
        [_relation("cause", "effect", "caused_by")],
        {"cause": _ref("cause"), "effect": _ref("effect")},
        resolver,
        supports=lambda key: [],
        read=lambda ref: Mock(content=ref.unit_id),
    )
    assert len(records) == 1
    assert records[0].relation_type == "possible_cause_of"
    assert records[0].source.unit_id == "effect"  # caused_by reverses direction
    assert records[0].target.unit_id == "cause"
    assert records[0].status == RelationStatus.CANDIDATE
    resolver.resolve.assert_not_called()  # non-conflicts skip the resolver


def test_unmappable_legacy_relations_are_not_promoted():
    records = resolve_discovered_conflicts(
        [_relation("a", "b", "similar_to"), _relation("a", "b", "refers_to")],
        {"a": _ref("a"), "b": _ref("b")},
        Mock(),
        supports=lambda key: [],
        read=lambda ref: Mock(content=ref.unit_id),
    )
    assert records == []


def test_supports_selection_records_proof_reference():
    proof = _ref("proof")
    b = _ref("b")
    confirmed_support = RelationRecord(
        "s", proof, b, "supports", status=RelationStatus.CONFIRMED,
        evidence_refs=[proof], conditions=[],
    )
    resolver = Mock()
    resolver.resolve.return_value = Resolution(D.PREFER_B, "supported")

    records = resolve_discovered_conflicts(
        [_relation("a", "b", "contradicts")],
        {"a": _ref("a"), "b": b},
        resolver,
        supports=lambda key: [(confirmed_support, proof)] if key == b.key else [],
        read=lambda ref: Mock(content=ref.unit_id),
    )
    assert records[0].resolution_refs == [proof]
    assert asdict(records[0])["resolution_refs"][0]["unit_id"] == "proof"
