"""Explicit mapping preserves legacy relation semantics."""

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.continuity import MemoryRef

pytestmark = pytest.mark.unit


def test_legacy_cause_mapping_reverses_direction_without_confirmation():
    from jiuwen_memory.common.relations.legacy import evidence_candidate
    from jiuwen_memory.common.type_def import Relation

    scope = Scope(org="acme", space="project")
    a, b = [MemoryRef(scope, unit_id, "0" * 64) for unit_id in ("effect", "cause")]
    relation = evidence_candidate(
        Relation("a", "b", "caused_by"), {"a": a, "b": b}, relation_id="legacy-cause"
    )
    assert relation.source == b and relation.target == a
    assert relation.relation_type == "possible_cause_of"
    assert relation.status.value == "candidate"
    assert evidence_candidate(Relation("a", "b", "similar_to"), {}, relation_id="ignored") is None
    with pytest.raises(ValidationError):
        evidence_candidate(Relation("a", "b", "refers_to"), {}, relation_id="unmapped")
