"""Relation coverage manifest: registered build range, projection watermark."""

import pytest

from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, RoleViewQuery

pytestmark = pytest.mark.unit


def _view(service, scope, read, anchor):
    return service.view(scope, RoleViewQuery(anchor=anchor), read, lambda *_: [])


def test_no_registered_relations_is_unknown(setup):
    service, scope, write, read, _, _ = setup
    anchor = MemoryRef.of(write("a fact")[0])
    result = _view(service, scope, read, anchor)
    assert result["meta"]["relation_coverage"] == "unknown"


def test_projected_relation_is_covered_and_manifest_recorded(setup):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("observation", "judgment")]
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    result = _view(service, scope, read, a)
    assert result["meta"]["relation_coverage"] == "covered_for_requested_scope"
    manifest = service.repository.get(scope, "coverage", "relations")
    assert manifest["relation_count"] == 1
    assert manifest["projected_count"] == 1
    assert manifest["model_version"]
    assert manifest["source_digest"]
    assert manifest["projection_watermark"] == 1


def test_unprojected_relation_is_partial(setup, monkeypatch):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("observation", "judgment")]

    def fail(units, *, mode):
        raise RuntimeError("graph projection down")

    monkeypatch.setattr(service.projector, "build", fail)
    outcome = service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    assert outcome["status"] == "partial"
    assert _view(service, scope, read, a)["meta"]["relation_coverage"] == "partial"


def test_repair_catches_up_surveying_only_completed_range(setup, monkeypatch):
    service, scope, write, read, _, _ = setup
    a, b = [MemoryRef.of(write(t)[0]) for t in ("observation", "judgment")]

    def fail(units, *, mode):
        raise RuntimeError("graph projection down")

    monkeypatch.setattr(service.projector, "build", fail)
    service.relation(
        scope,
        RelationRecord("r", a, b, "supports", status="confirmed", evidence_refs=[a]),
        0, read, write,
    )
    monkeypatch.undo()
    repair = service.repair(scope, read)
    assert repair["coverage"] == "covered_for_requested_scope"
    assert _view(service, scope, read, a)["meta"]["relation_coverage"] == (
        "covered_for_requested_scope"
    )
    # Traversal finishing never upgrades the top-level completeness judgement.
    assert _view(service, scope, read, a)["meta"]["completeness"] == "partial"
