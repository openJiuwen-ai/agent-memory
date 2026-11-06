"""Conflict consolidation pipeline: discovered contradictions become candidate evidence.

The old LLMAssociator reports legacy relation types (``caused_by`` / ``contradicts``).
This module converts the mappable subset into candidate ``RelationRecord`` instances and,
for contradictions, invokes the injected ``ConflictResolver``. The result is attached as a
resolution recommendation but the relation stays ``candidate`` — the memory system never
auto-confirms an association or a conflict; a human review or an explicit evidence-backed
``continuity_relation`` confirmation remains required.
"""

from __future__ import annotations

from collections.abc import Callable

from jiuwen_memory.common.relations.legacy import evidence_candidate
from jiuwen_memory.common.type_def import MemoryUnit, Relation
from jiuwen_memory.common.type_def.continuity import MemoryRef, RelationRecord, RelationStatus
from jiuwen_memory.construction.conflict_resolver import ConflictResolver

# The subset of legacy association types that map onto evidence relations (see legacy.py).
_MAPPABLE = frozenset({"caused_by", "contradicts"})


def resolve_discovered_conflicts(
    relations: list[Relation],
    references: dict[str, MemoryRef],
    resolver: ConflictResolver,
    *,
    supports: Callable[[str], list[tuple[RelationRecord, MemoryRef]]],
    read: Callable[[MemoryRef], MemoryUnit],
    sources: Callable[[str], str] | None = None,
    times: Callable[[str], object] | None = None,
    batch_id: str = "",
) -> list[RelationRecord]:
    """Convert mappable legacy associations and resolve contradictions conservatively.

    ``supports(key)`` returns authorized relation records and their persisted references,
    used to decide same-condition positive support (``verifies`` never counts). All outputs
    keep the candidate status: a resolved recommendation does not auto-confirm anything.
    """
    records: list[RelationRecord] = []
    for index, relation in enumerate(relations):
        if relation.relation not in _MAPPABLE:
            # similar_to / corefers stay in their original indexes; other legacy types are
            # not implicitly promoted into evidence relations.
            continue
        record = evidence_candidate(
            relation, references, relation_id=f"assoc-{index}", batch_id=batch_id
        )
        if record is None:
            continue
        if record.relation_type != "contradicts":
            records.append(record)
            continue
        context = {
            "same_conditions": bool(record.conditions),
            "evidence_complete": True,
            "multiple_conflicts": False,
            "explicit_correction": False,
        }
        proof_refs: list[MemoryRef] = []
        for suffix, ref in (("a", record.source), ("b", record.target)):
            context[f"source_{suffix}"] = sources(ref.key) if sources else ""
            context[f"time_{suffix}"] = times(ref.key) if times else None
            verified = False
            for other, proof_ref in supports(ref.key):
                confirmed_support = (
                    other.relation_type == "supports"
                    and other.status == RelationStatus.CONFIRMED
                )
                matching_evidence = other.conditions == record.conditions and other.evidence_refs
                if confirmed_support and other.target == ref and matching_evidence:
                    verified = True
                    proof_refs.append(proof_ref)
                    break
            context[f"verified_{suffix}"] = verified
        result = resolver.resolve(read(record.source), read(record.target), record, context)
        record.resolution = "escalate" if result.candidate else result.decision.value
        record.resolution_reason = result.reason
        record.resolution_origin = "model_candidate" if result.candidate else "automatic"
        record.resolution_refs = (
            proof_refs if result.decision.value in {"prefer_a", "prefer_b"} else []
        )
        records.append(record)
    return records
