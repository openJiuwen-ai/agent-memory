"""Legacy association mapping: caused_by/contradicts become candidate evidence relations."""

import json
from typing import Any, NamedTuple
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.type_def import Relation
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    TaskRequest,
    fingerprint,
)
from jiuwen_memory.construction.conflict_resolver import ConflictResolution, Resolution

pytestmark = pytest.mark.unit


class AssociationFixture(NamedTuple):
    service: Any
    scope: Any
    event: Any
    write: Any
    read: Any
    units: Any
    source: Any
    target: Any


def _setup_with_associator(setup, relations):
    service, scope, write, read, _, units = setup
    a = write("claim A")[0]
    b = write("claim B")[0]
    task = service.task(
        scope,
        TaskRequest("create", "create", background=[MemoryRef.of(a), MemoryRef.of(b)]),
        "owner",
        read,
    )
    binding = service.task(
        scope,
        TaskRequest("bind", "bind", task["task_id"], 1, connector_id="wb", context_id="s"),
        "owner",
        read,
    )
    associator = Mock()
    associator.associate.return_value = relations
    service.associator = associator
    service.conflict_resolver = Mock()
    service.conflict_resolver.resolve.return_value = Resolution(
        ConflictResolution.PREFER_A, "evidence favours A"
    )
    event = ContinuityEvent(
        task["task_id"], binding["task"]["bindings"][0]["binding_id"], "event", "source"
    )
    return AssociationFixture(service, scope, event, write, read, units, a, b)


def _candidate_types(service, scope, event):
    key = fingerprint({"connector": "wb", "binding": event.binding_id, "event": event.event_id})
    return {
        c["relation_type"]: c
        for c in service.repository.get(scope, "events", key)["relation_candidates"]
    }


def _written_relations(service, scope, read):
    out = []
    for head in service.repository.list(scope, "relations"):
        if head.get("ref"):
            out.append(RelationRecord.parse(json.loads(read(MemoryRef.parse(head["ref"])).content)))
    return out


def test_legacy_types_are_mapped_into_candidate_evidence(setup):
    relations = [
        Relation(relation="contradicts"),
        Relation(relation="similar_to"),
        Relation(relation="caused_by"),
    ]
    service, scope, event, write, read, units, a, b = _setup_with_associator(setup, relations)
    relations[0].source_id, relations[0].target_id = a.id, b.id
    relations[1].source_id, relations[1].target_id = a.id, b.id
    relations[2].source_id, relations[2].target_id = b.id, a.id

    receipt = service.record(scope, event, "wb", read, write)
    assert receipt["association_status"] == "committed"

    candidates = _candidate_types(service, scope, event)
    assert set(candidates) == {"contradicts", "possible_cause_of"}  # similar_to dropped
    assert candidates["contradicts"]["status"] == RelationStatus.CANDIDATE.value

    written = {r.relation_type: r for r in _written_relations(service, scope, read)}
    # The injected resolver resolves the discovered contradict, but never auto-confirms it.
    assert written["contradicts"].status == RelationStatus.CANDIDATE
    assert written["contradicts"].resolution == "prefer_a"
    service.conflict_resolver.resolve.assert_called_once()


def test_caused_by_swaps_endpoints_to_possible_cause_of(setup):
    relations = [Relation(relation="caused_by")]
    service, scope, event, write, read, units, a, b = _setup_with_associator(setup, relations)
    relations[0].source_id, relations[0].target_id = b.id, a.id  # "b caused a"
    service.record(scope, event, "wb", read, write)
    caused = _candidate_types(service, scope, event)["possible_cause_of"]
    # caused_by(b, a) maps to possible_cause_of(a, b): a is a possible cause of b.
    assert caused["source"]["unit_id"] == a.id
    assert caused["target"]["unit_id"] == b.id
    service.conflict_resolver.resolve.assert_not_called()  # non-conflicts skip the resolver


def test_non_mappable_legacy_types_are_skipped_without_resolver(setup):
    relations = [Relation(relation="corefers")]
    service, scope, event, write, read, units, a, b = _setup_with_associator(setup, relations)
    relations[0].source_id, relations[0].target_id = a.id, b.id
    service.conflict_resolver = None
    receipt = service.record(scope, event, "wb", read, write)
    assert receipt["association_status"] == "committed"
    assert _candidate_types(service, scope, event) == {}


@pytest.mark.parametrize("failure", ["unavailable", "bug"])
def test_support_lookup_failure_is_not_silently_treated_as_no_support(setup, monkeypatch, failure):
    from jiuwen_memory.common.errors import NotFoundError

    relations = [Relation(relation="contradicts")]
    fixture = _setup_with_associator(setup, relations)
    relations[0].source_id = fixture.source.id
    relations[0].target_id = fixture.target.id
    fixture.service.relation(
        fixture.scope,
        RelationRecord(
            "support", MemoryRef.of(fixture.source), MemoryRef.of(fixture.target),
            "supports", status="confirmed", evidence_refs=[MemoryRef.of(fixture.source)],
        ),
        0, fixture.read, fixture.write,
    )
    error = (
        NotFoundError("relation", "missing") if failure == "unavailable" else RuntimeError("bug")
    )
    monkeypatch.setattr(fixture.service, "resolve_edge", Mock(side_effect=error))

    def map_candidates():
        # Exercise the legacy-only path directly; catalog routing is tested separately.
        # pylint: disable-next=protected-access
        return fixture.service._map_legacy_relations(
            fixture.scope, "batch", relations,
            {fixture.source.id: fixture.source, fixture.target.id: fixture.target},
            fixture.read,
        )

    if failure == "bug":
        with pytest.raises(RuntimeError, match="bug"):
            map_candidates()
        fixture.service.conflict_resolver.resolve.assert_not_called()
    else:
        assert len(map_candidates()) == 1
        context = fixture.service.conflict_resolver.resolve.call_args.args[-1]
        assert context["verified_a"] is False
        assert context["verified_b"] is False
