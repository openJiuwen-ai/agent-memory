# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pipeline adapter that projects TemporalEntity views back to MemoryUnits."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from threading import local

from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import (
    ChannelEvidence,
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    RecallChannel,
    Scope,
    ScoredMemoryUnit,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.vector import VectorStore

from .model import EventTimePrecision, SchemaPropertyEntry, SchemaTemporalResult
from .query import temporal_query_from_extensions
from .reader import PropertyIdLookup
from .searcher import SchemaTemporalSearcher, lexical_property_score


class TemporalEntitySelector:
    """Optional selection stage for ``PipelineRetriever``.

    TemporalEntity is an internal assembly only.  The returned candidates and
    unit map always point at persisted Property or Source MemoryUnits, keeping
    ``get(unit_id)``, disclosure, filtering and provenance semantics intact.
    """

    def __init__(
        self,
        domain_store: DomainStore,
        *,
        auto_enabled: bool = False,
        property_id_lookup: PropertyIdLookup | None = None,
        entity_fulltext_store: FulltextStore | None = None,
        entity_vector_store: VectorStore | None = None,
        entity_top_k: int = 20,
        property_top_k: int = 50,
        property_top_n: int = 25,
        rrf_k: int = 60,
        max_properties_per_entity: int = 20,
        property_allocation_min_factor: float = 0.5,
        property_allocation_max_factor: float = 1.5,
        property_extension_step: int = 3,
        property_reranker: Reranker | None = None,
        property_rerank_enabled: bool = True,
        shrink_enabled: bool = True,
        direct_property_rerank_enabled: bool = False,
        entity_rerank_enabled: bool = False,
        entity_rerank_max_chars: int = 4000,
        source_fallback_enabled: bool = True,
        source_policy: str = "missing_or_incomplete",
        source_top_k: int = 20,
        source_ratio: float = 0.3,
    ) -> None:
        self._searcher = SchemaTemporalSearcher(
            domain_store,
            auto_enabled=auto_enabled,
            property_id_lookup=property_id_lookup,
            entity_fulltext_store=entity_fulltext_store,
            entity_vector_store=entity_vector_store,
            entity_top_k=entity_top_k,
            property_top_k=property_top_k,
            property_top_n=property_top_n,
            rrf_k=rrf_k,
            max_properties_per_entity=max_properties_per_entity,
            property_allocation_min_factor=property_allocation_min_factor,
            property_allocation_max_factor=property_allocation_max_factor,
            property_extension_step=property_extension_step,
            property_reranker=property_reranker,
            property_rerank_enabled=property_rerank_enabled,
            shrink_enabled=shrink_enabled,
            direct_property_rerank_enabled=direct_property_rerank_enabled,
            entity_rerank_enabled=entity_rerank_enabled,
            entity_rerank_max_chars=entity_rerank_max_chars,
        )
        self._source_policy = _normalize_source_policy(
            source_policy if source_fallback_enabled else "none"
        )
        self._source_top_k = max(0, int(source_top_k))
        self._source_ratio = min(1.0, max(0.0, float(source_ratio)))
        self._state = local()

    @property
    def last_result(self) -> SchemaTemporalResult | None:
        return getattr(self._state, "result", None)

    @last_result.setter
    def last_result(self, value: SchemaTemporalResult | None) -> None:
        self._state.result = value

    @property
    def last_source_candidates(self) -> list[ScoredMemoryUnit]:
        return list(getattr(self._state, "source_candidates", []))

    @last_source_candidates.setter
    def last_source_candidates(self, value: list[ScoredMemoryUnit]) -> None:
        self._state.source_candidates = list(value)

    @property
    def source_top_k(self) -> int:
        return self._source_top_k

    @property
    def source_ratio(self) -> float:
        return self._source_ratio

    @property
    def recall_errors(self):
        """Return partial failures collected by the schema Property path."""

        return list(getattr(self._state, "recall_errors", []))

    def should_select(self, parsed: ParsedQuery) -> bool:
        explicit = temporal_query_from_extensions(parsed)
        return self._searcher.should_search(parsed, explicit)

    def select(
        self,
        scope: Scope,
        parsed: ParsedQuery,
        candidates: list[ScoredMemoryUnit],
        *,
        filters: FilterExpr | None = None,
        limit: int | None = None,
    ) -> tuple[list[ScoredMemoryUnit], dict[str, MemoryUnit]]:
        """Select and order real units for the schema-temporal branch."""

        explicit = temporal_query_from_extensions(parsed)
        result = self._searcher.search(
            scope,
            parsed,
            explicit,
            seed_entity_ids=_seed_entity_ids(candidates),
            filters=filters,
        )
        self._state.recall_errors = self._searcher.recall_errors
        self.last_result = result
        self.last_source_candidates = []
        if result is None:
            return list(candidates), {candidate.unit_id: candidate.unit for candidate in candidates}

        existing = {candidate.unit_id: candidate for candidate in candidates}
        selected_ids = _ordered_property_ids(result, parsed)
        selected_units = self._searcher.reader.load_units(scope, selected_ids)
        selected = _property_candidates(result, parsed, selected_ids, selected_units, existing)

        selected = _deduplicate_and_sort(selected)
        sources = _source_fallback_candidates(
            result,
            candidates,
            policy=self._source_policy,
        )
        self.last_source_candidates = list(sources)
        result_limit = max(1, int(limit or len(candidates) or len(selected) or 1))
        selected = _merge_property_and_source_candidates(
            selected,
            sources,
            limit=result_limit,
            source_top_k=self._source_top_k,
            source_ratio=self._source_ratio,
        )
        if not selected:
            selected = list(candidates[:result_limit])
        return selected, {candidate.unit_id: candidate.unit for candidate in selected}


def _ordered_property_ids(
    result: SchemaTemporalResult,
    parsed: ParsedQuery,
) -> list[str]:
    direct_scores = result.direct_property_scores
    entity_position = {entity.entity_id: index for index, entity in enumerate(result.entities)}
    rows = []
    for entity in result.entities:
        for property_name, timeline in entity.properties.items():
            for entry in timeline:
                rows.append(
                    (
                        entry.unit_id,
                        direct_scores.get(entry.unit_id, 0.0),
                        lexical_property_score(parsed, property_name, entry.value),
                        entity_position[entity.entity_id],
                    )
                )
    rows.sort(key=lambda row: (-row[1], -row[2], row[3], row[0]))
    return list(dict.fromkeys(row[0] for row in rows))


def _seed_entity_ids(candidates: list[ScoredMemoryUnit]) -> list[str]:
    """Extract canonical entity seeds from the final ordinary result order."""

    seeds: list[str] = []
    for candidate in candidates:
        metadata = candidate.unit.system_metadata
        entity_id = str(
            metadata.get("schema_entity_id") or metadata.get("schema_entity_key") or ""
        ).strip()
        if entity_id:
            seeds.append(entity_id)
        raw_keys = metadata.get("schema_entity_keys")
        if isinstance(raw_keys, list):
            seeds.extend(str(value).strip() for value in raw_keys if str(value).strip())
    return list(dict.fromkeys(seeds))


def _property_candidates(
    result: SchemaTemporalResult,
    parsed: ParsedQuery,
    unit_ids: list[str],
    units: dict[str, MemoryUnit],
    existing: dict[str, ScoredMemoryUnit],
) -> list[ScoredMemoryUnit]:
    entity_score_by_unit = {
        entry.unit_id: result.entity_scores.get(entity.entity_id, 0.0)
        for entity in result.entities
        for timeline in entity.properties.values()
        for entry in timeline
    }
    property_name_by_unit = {
        entry.unit_id: property_name
        for entity in result.entities
        for property_name, timeline in entity.properties.items()
        for entry in timeline
    }
    output: list[ScoredMemoryUnit] = []
    for rank, unit_id in enumerate(unit_ids, start=1):
        unit = units.get(unit_id)
        if unit is None:
            continue
        lexical = lexical_property_score(
            parsed,
            property_name_by_unit.get(unit_id, ""),
            unit.content,
        )
        score = max(
            result.direct_property_scores.get(unit_id, 0.0),
            entity_score_by_unit.get(unit_id, 0.0),
        )
        score += lexical * 0.001
        prior = existing.get(unit_id)
        if prior is not None:
            output.append(
                replace(
                    prior,
                    score=max(prior.score, score),
                    channel=RecallChannel.TEMPORAL,
                )
            )
            continue
        evidence = [
            ChannelEvidence(
                channel=RecallChannel.TEMPORAL,
                rank=rank,
                score=score,
                contribution=score,
            )
        ]
        output.append(ScoredMemoryUnit(unit, score, RecallChannel.TEMPORAL, evidence))
    return output


def _source_fallback_candidates(
    result: SchemaTemporalResult,
    ordinary_candidates: list[ScoredMemoryUnit],
    *,
    policy: str = "missing_or_incomplete",
) -> list[ScoredMemoryUnit]:
    """Keep already-authorized Source hits when direct Property evidence is incomplete."""

    normalized_policy = _normalize_source_policy(policy)
    if normalized_policy == "none":
        return []

    direct_ids = {
        unit_id for unit_ids in result.direct_property_unit_ids.values() for unit_id in unit_ids
    }
    entries_by_source: dict[str, list[SchemaPropertyEntry]] = {}
    for entity in result.entities:
        for timeline in entity.properties.values():
            for entry in timeline:
                if entry.unit_id not in direct_ids:
                    continue
                for source_id in entry.provenance:
                    if source_id:
                        entries_by_source.setdefault(source_id, []).append(entry)

    sources: list[ScoredMemoryUnit] = []
    for candidate in ordinary_candidates:
        unit = candidate.unit
        if not _is_source_evidence(unit):
            continue
        entries = entries_by_source.get(unit.id, [])
        if normalized_policy == "missing" and entries:
            continue
        if (
            normalized_policy == "missing_or_incomplete"
            and any(_property_covers_source(entry, unit) for entry in entries)
        ):
            continue
        sources.append(candidate)
    return _deduplicate_and_sort(sources)


_SOURCE_POLICIES = {"none", "missing", "missing_or_incomplete", "always"}


def _normalize_source_policy(value: str) -> str:
    policy = str(value or "").strip().casefold()
    if policy not in _SOURCE_POLICIES:
        choices = ", ".join(sorted(_SOURCE_POLICIES))
        raise ValueError(f"schema temporal source policy must be one of: {choices}")
    return policy


def _merge_property_and_source_candidates(
    properties: list[ScoredMemoryUnit],
    sources: list[ScoredMemoryUnit],
    *,
    limit: int,
    source_top_k: int,
    source_ratio: float,
) -> list[ScoredMemoryUnit]:
    """Reserve bounded candidate slots for Source-first durability evidence."""

    if not sources or source_top_k <= 0 or source_ratio <= 0:
        return properties[:limit]
    if not properties:
        return sources[: min(limit, source_top_k)]

    source_budget = min(
        len(sources),
        source_top_k,
        max(1, math.ceil(limit * source_ratio)),
    )
    if limit == 1:
        return properties[:1]
    source_budget = min(source_budget, limit - 1)
    property_budget = limit - source_budget
    merged = [*properties[:property_budget], *sources[:source_budget]]
    if len(merged) < limit:
        merged.extend(properties[property_budget : property_budget + limit - len(merged)])
    if len(merged) < limit:
        merged.extend(sources[source_budget : source_budget + limit - len(merged)])
    return _deduplicate_and_sort(merged)[:limit]


_RELATIVE_TIME_PATTERNS = (
    re.compile(r"\b(?:today|yesterday|tomorrow|tonight)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:last|next|this)\s+"
        r"(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:\d+|one|two|three|four|five|six|seven)\s+"
        r"(?:days?|weeks?|months?|years?)\s+ago\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:今天|昨天|明天|上周|下周|本周|上个月|下个月|本月|去年|明年|今年)"),
)


def _is_source_evidence(unit: MemoryUnit) -> bool:
    metadata = unit.system_metadata
    return bool(
        metadata.get("memory_role") == "source_evidence"
        or metadata.get("schema_source_evidence") is True
        or str(metadata.get("schema_source_evidence") or "").casefold() == "true"
    )


def _property_covers_source(entry: SchemaPropertyEntry, source: MemoryUnit) -> bool:
    event_anchor = entry.event_time or entry.event_start
    if event_anchor is None or entry.event_precision is EventTimePrecision.UNKNOWN:
        return False
    markers = _relative_time_markers(source.content)
    if not markers:
        return True
    value = entry.value.casefold()
    if not all(marker.casefold() in value for marker in markers):
        return False
    return _precision_rank(entry.event_precision) >= _required_relative_precision(markers)


def _relative_time_markers(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(0).strip()
            for pattern in _RELATIVE_TIME_PATTERNS
            for match in pattern.finditer(text)
        )
    )


def _required_relative_precision(markers: list[str]) -> int:
    lowered = " ".join(markers).casefold()
    day_pattern = (
        r"today|yesterday|tomorrow|tonight|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday|\bdays?\b|今天|昨天|明天"
    )
    if re.search(day_pattern, lowered):
        return _precision_rank(EventTimePrecision.DAY)
    if re.search(r"week|month|周|月", lowered):
        return _precision_rank(EventTimePrecision.MONTH)
    return _precision_rank(EventTimePrecision.YEAR)


def _precision_rank(precision: EventTimePrecision) -> int:
    return {
        EventTimePrecision.UNKNOWN: 0,
        EventTimePrecision.YEAR: 1,
        EventTimePrecision.MONTH: 2,
        EventTimePrecision.DAY: 3,
        EventTimePrecision.DATETIME: 4,
    }[precision]


def _deduplicate_and_sort(candidates: list[ScoredMemoryUnit]) -> list[ScoredMemoryUnit]:
    by_id: dict[str, ScoredMemoryUnit] = {}
    for candidate in candidates:
        current = by_id.get(candidate.unit_id)
        if current is None or candidate.score > current.score:
            by_id[candidate.unit_id] = candidate
    return sorted(by_id.values(), key=lambda candidate: (-candidate.score, candidate.unit_id))
