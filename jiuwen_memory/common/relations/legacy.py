"""Explicit legacy Association mapping; co-occurrence never becomes evidence."""

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Relation
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord


def evidence_candidate(
    relation: Relation, references: dict[str, MemoryRef], *, relation_id: str, batch_id: str = ""
) -> RelationRecord | None:
    """Convert a resolved legacy association for selective backfill, never auto-confirm it."""
    if relation.relation in {"similar_to", "corefers"}:
        return None  # Similarity/entity identity stay in their original indexes.
    if relation.relation not in {"caused_by", "contradicts"}:
        raise ValidationError("legacy relation requires explicit semantic mapping")
    try:
        source, target = references[relation.source_id], references[relation.target_id]
    except KeyError:
        raise ValidationError(
            "legacy endpoints must first resolve to authorized MemoryRefs"
        ) from None
    if relation.relation == "caused_by":
        source, target = target, source
        kind = "possible_cause_of"
    else:
        kind = "contradicts"
    return RelationRecord(relation_id, source, target, kind, batch_id=batch_id)
