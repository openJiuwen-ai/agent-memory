"""wikimem baseline consolidation for mem2.0 MemoryUnit records."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from common.type_def import LifecycleState, MemoryTier, MemoryUnit, Segment
from construction.base import OperatorType
from construction.evolver import EvolveMode, EvolveResult, Evolver, EvolverProducer
from construction.extractor_impl.wikimem_baseline_extractor import (
    WIKIMEM_ACTION,
    WIKIMEM_KEY,
    WIKIMEM_MEMORY_TYPE,
    WIKIMEM_OBSERVED_AT_MS,
    WIKIMEM_SCORE,
    WIKIMEM_SKIP_REASON,
    WIKIMEM_SOURCE_MESSAGE_ID,
    WIKIMEM_VALUE,
)

WIKIMEM_DESCRIPTION = "wikimem.description"
WIKIMEM_KIND = "wikimem.kind"
WIKIMEM_RECORD_KIND = "record"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class WikimemBaselineOutcome:
    """Consolidation output plus diagnostics for skipped candidates."""

    units: list[MemoryUnit] = field(default_factory=list)
    result: EvolveResult = field(default_factory=EvolveResult)
    skipped_ids: list[str] = field(default_factory=list)


class WikimemBaselineEvolver(Evolver):
    """Apply wikimem baseline upsert/forget candidates to MemoryUnit records."""

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        if mode != EvolveMode.CONSOLIDATE:
            return EvolveResult()
        prior = [unit for unit in units if not is_wikimem_candidate(unit)]
        candidates = [unit for unit in units if is_wikimem_candidate(unit)]
        return self.consolidate(prior, candidates).result

    def consolidate(
        self, prior: list[MemoryUnit], candidates: list[MemoryUnit]
    ) -> WikimemBaselineOutcome:
        outcome = WikimemBaselineOutcome(units=[deepcopy(unit) for unit in prior])
        for candidate in candidates:
            metadata = candidate.metadata
            if metadata.get(WIKIMEM_SKIP_REASON):
                outcome.skipped_ids.append(candidate.id)
                continue
            action = metadata.get(WIKIMEM_ACTION)
            if action == "upsert":
                self._upsert(outcome, candidate)
            elif action == "forget":
                self._forget(outcome, candidate)
        return outcome

    def _upsert(self, outcome: WikimemBaselineOutcome, candidate: MemoryUnit) -> None:
        key = candidate.metadata[WIKIMEM_KEY]
        supersedes = ""
        for unit in outcome.units:
            if (
                is_wikimem_record(unit)
                and unit.lifecycle == LifecycleState.ACTIVE
                and unit.metadata.get(WIKIMEM_KEY) == key
            ):
                unit.lifecycle = LifecycleState.SUPERSEDED
                outcome.result.superseded_ids.append(unit.id)
                supersedes = unit.id

        record = _record_from_candidate(candidate, supersedes=supersedes)
        outcome.units.append(record)
        outcome.result.created_ids.append(record.id)

    def _forget(self, outcome: WikimemBaselineOutcome, candidate: MemoryUnit) -> None:
        key = candidate.metadata[WIKIMEM_KEY]
        value = candidate.metadata[WIKIMEM_VALUE]
        normalized_value = _normalize_memory_text(value)
        for unit in outcome.units:
            if not is_wikimem_record(unit) or unit.lifecycle == LifecycleState.FORGOTTEN:
                continue
            if (
                unit.metadata.get(WIKIMEM_KEY) == key
                or _normalize_memory_text(unit.content) == normalized_value
            ):
                unit.lifecycle = LifecycleState.FORGOTTEN
                outcome.result.forgotten_ids.append(unit.id)


def is_wikimem_candidate(unit: MemoryUnit) -> bool:
    return "wikimem_candidate" in unit.tags and WIKIMEM_ACTION in unit.metadata


def is_wikimem_record(unit: MemoryUnit) -> bool:
    return unit.metadata.get(WIKIMEM_KIND) == WIKIMEM_RECORD_KIND


def _record_from_candidate(candidate: MemoryUnit, *, supersedes: str) -> MemoryUnit:
    metadata = dict(candidate.metadata)
    memory_type = metadata.get(WIKIMEM_MEMORY_TYPE, "project")
    metadata[WIKIMEM_KIND] = WIKIMEM_RECORD_KIND
    metadata[WIKIMEM_DESCRIPTION] = _format_memory_description(metadata)
    tags = [tag for tag in candidate.tags if tag != "wikimem_candidate"]
    if "wikimem_record" not in tags:
        tags.append("wikimem_record")
    return MemoryUnit(
        id=f"wikimem_record_{uuid.uuid4().hex}",
        scope=candidate.scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=metadata[WIKIMEM_VALUE], source=candidate.source)],
        source_ref=metadata.get(WIKIMEM_SOURCE_MESSAGE_ID, ""),
        temporal=candidate.temporal,
        provenance=list(candidate.provenance),
        supersedes=supersedes,
        tags=tags,
        metadata=metadata,
        lifecycle=LifecycleState.ACTIVE,
    )


def _format_memory_description(metadata: dict[str, str]) -> str:
    memory_type = metadata.get(WIKIMEM_MEMORY_TYPE, "general")
    source = metadata.get(WIKIMEM_SOURCE_MESSAGE_ID, "unknown-message")
    return (
        f"remembered {memory_type} context from {source}: "
        f"{metadata[WIKIMEM_KEY]} = {metadata[WIKIMEM_VALUE]}"
    )


def _normalize_memory_text(text: str) -> str:
    return " ".join(match.group(0).lower() for match in _TOKEN_RE.finditer(text))


@EvolverProducer.register("wikimem_baseline")
def _build(config):
    return WikimemBaselineEvolver()
