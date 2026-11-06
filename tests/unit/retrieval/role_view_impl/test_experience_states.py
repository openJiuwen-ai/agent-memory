"""Experience semantic states: candidate → supported → review → refuted, revocable."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, RoleViewQuery
from jiuwen_memory.construction.index_builder_impl.evidence_graph import EvidenceGraphBuilder
from jiuwen_memory.control.continuity_impl.repository import ContinuityRepository
from jiuwen_memory.control.continuity_impl.service import ContinuityService
from jiuwen_memory.retrieval.role_view_impl.bounded import RoleViewBuilder
from jiuwen_memory.storage.graph_impl.in_memory_graph_store import InMemoryGraphStore
from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

pytestmark = pytest.mark.unit


@pytest.fixture
def service(tmp_path):
    kv = SQLiteKVStore(str(tmp_path / "registry.db"))
    graph = InMemoryGraphStore()
    svc = ContinuityService(
        ContinuityRepository(kv, single_writer=True),
        graph,
        RoleViewBuilder(),
        projector=EvidenceGraphBuilder(graph),
    )
    scope = Scope(org="o", space="s")
    units: dict[str, MemoryUnit] = {}

    def write(text, metadata=None):
        unit = MemoryUnit(
            id=str(len(units) + 1), scope=scope, segments=[Segment(content=text)],
            system_metadata=metadata or {},
        )
        units[unit.id] = unit
        return [unit]

    def read(ref):
        unit = units[ref.unit_id]
        if MemoryRef.of(unit) != ref:
            raise AssertionError("content changed")
        return unit

    yield svc, scope, write, read
    kv.close()


def _view(service, scope, read, anchor):
    return service.view(scope, RoleViewQuery(anchor=anchor), read, lambda *_: [])["experiences"]


def test_experience_is_candidate_until_supported(service):
    svc, scope, write, read = service
    exp = MemoryRef.of(write("retry on timeout", {"continuity_kind": "experience"})[0])
    assert _view(svc, scope, read, exp)[0]["status"] == "candidate"


def test_experience_support_elevates_and_preserves_conditions(service):
    svc, scope, write, read = service
    exp = MemoryRef.of(write("retry on timeout", {"continuity_kind": "experience"})[0])
    evidence = MemoryRef.of(write("passing run under v2")[0])
    svc.relation(
        scope,
        RelationRecord(
            "s", evidence, exp, "supports", status="confirmed",
            evidence_refs=[evidence], conditions=["version=2", "load=100"],
        ),
        0, read, write,
    )
    item = _view(svc, scope, read, exp)[0]
    assert item["status"] == "supported_in_conditions"
    assert item["conditions"] == ["version=2", "load=100"]


def test_experience_unresolved_counterexample_downgrades(service):
    svc, scope, write, read = service
    exp = MemoryRef.of(write("retry on timeout", {"continuity_kind": "experience"})[0])
    evidence = MemoryRef.of(write("passing run")[0])
    counter = MemoryRef.of(write("failing run")[0])
    svc.relation(
        scope,
        RelationRecord(
            "s", evidence, exp, "supports", status="confirmed",
            evidence_refs=[evidence], conditions=["v2"],
        ),
        0, read, write,
    )
    svc.relation(
        scope,
        RelationRecord(
            "c", counter, exp, "contradicts", status="confirmed",
            evidence_refs=[counter], conditions=["v2"], conflict_kind="factual",
        ),
        0, read, write,
    )
    item = _view(svc, scope, read, exp)[0]
    assert item["status"] == "needs_review"


def test_experience_support_revocation_returns_to_candidate(service):
    svc, scope, write, read = service
    exp = MemoryRef.of(write("retry on timeout", {"continuity_kind": "experience"})[0])
    evidence = MemoryRef.of(write("passing run")[0])
    relation = RelationRecord(
        "s", evidence, exp, "supports", status="confirmed",
        evidence_refs=[evidence], conditions=["v2"],
    )
    svc.relation(scope, relation, 0, read, write)
    assert _view(svc, scope, read, exp)[0]["status"] == "supported_in_conditions"
    svc.relation(scope, replace(relation, revision=2, status="revoked"), 1, read, write)
    assert _view(svc, scope, read, exp)[0]["status"] == "candidate"


def test_experience_explicit_negation_refutes(service):
    svc, scope, write, read = service
    exp = MemoryRef.of(write("retry on timeout", {"continuity_kind": "experience"})[0])
    counter = MemoryRef.of(write("definitive disproof")[0])
    svc.relation(
        scope,
        RelationRecord(
            "c", counter, exp, "contradicts", status="confirmed",
            evidence_refs=[counter], conditions=["v2"], conflict_kind="factual",
            resolution="prefer_a", resolution_reason="negated", resolution_origin="human",
        ),
        0, read, write,
    )
    assert _view(svc, scope, read, exp)[0]["status"] == "refuted"


def test_disputed_premise_downgrades_dependent_experience(service):
    svc, scope, write, read = service
    premise = MemoryRef.of(write("premise", {"continuity_kind": "judgment"})[0])
    exp = MemoryRef.of(write("procedure", {"continuity_kind": "experience"})[0])
    counter = MemoryRef.of(write("counterexample")[0])
    for record in [
        RelationRecord(
            "support", premise, exp, "supports", status="confirmed",
            evidence_refs=[premise], conditions=["v2"],
        ),
        RelationRecord(
            "counter", counter, premise, "contradicts", status="confirmed",
            evidence_refs=[counter], conditions=["v2"], conflict_kind="factual",
        ),
    ]:
        svc.relation(scope, record, 0, read, write)
    view = svc.view(scope, RoleViewQuery(anchor=premise), read, lambda *_: [])
    assert view["experiences"][0]["status"] == "needs_review"
    review = view["meta"]["review"]
    assert review["required"][0]["unit_id"] == premise.unit_id
    assert any(ref["unit_id"] == exp.unit_id for ref in review["candidate"])
    assert review["unchecked"] == ["dependencies_outside_view"]
