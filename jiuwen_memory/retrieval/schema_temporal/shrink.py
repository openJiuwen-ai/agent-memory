# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Budgeted property shrinking and post-shrink direct-hit restoration."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Callable, Protocol

from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import ParsedQuery

from .model import (
    SchemaPropertyEntry,
    SchemaShrinkDiagnostics,
    SchemaTemporalQuery,
    SchemaTemporalResult,
    TemporalEntity,
)


class PropertyRecallHit(Protocol):
    """Minimal structural type accepted from a property-local recall path."""

    unit_id: str
    score: float


EntityLocalRecall = Callable[[str], list[PropertyRecallHit]]


class SchemaTemporalShrinker:
    """Apply a bounded, query-aware budget only to hydrated entity timelines."""

    def __init__(
        self,
        *,
        max_properties_per_entity: int = 20,
        allocation_min_factor: float = 0.5,
        allocation_max_factor: float = 1.5,
        reranker: Reranker | None = None,
        rerank_enabled: bool = False,
    ) -> None:
        self._max_per_entity = max(1, int(max_properties_per_entity))
        self._allocation_min_factor = max(0.0, float(allocation_min_factor))
        self._allocation_max_factor = max(
            self._allocation_min_factor,
            float(allocation_max_factor),
        )
        self._reranker = reranker
        self._rerank_enabled = bool(rerank_enabled)

    def shrink(
        self,
        result: SchemaTemporalResult,
        query: ParsedQuery,
        *,
        entity_local_recall: EntityLocalRecall | None = None,
    ) -> SchemaTemporalResult:
        if not result.entities:
            result.shrink_diagnostics = SchemaShrinkDiagnostics()
            return result
        terms = _query_terms(query)
        query_text = query.rewritten or query.raw
        entry_counts = [_entry_count(entity) for entity in result.entities]
        entries_before = sum(entry_counts)
        budgets = self._allocate_budgets(entry_counts)
        entities_reranked = 0
        degraded_errors: set[str] = set()

        for entity, original_count, allocated in zip(
            result.entities,
            entry_counts,
            budgets,
            strict=True,
        ):
            budget = min(allocated, original_count)
            if budget <= 0:
                entity.properties = {}
                result.selected.pop(entity.entity_id, None)
                continue
            if original_count <= budget:
                result.selected[entity.entity_id] = entity.selected_payload(result.mode)
                continue
            entries = _flatten(entity)
            local_scores: dict[str, float] = {}
            if entity_local_recall is not None:
                try:
                    local_hits = entity_local_recall(entity.entity_id)
                except Exception as exc:
                    degraded_errors.add(type(exc).__name__)
                    local_hits = []
                for hit in local_hits:
                    local_scores[hit.unit_id] = hit.score
                locally_recalled = [item for item in entries if item[1].unit_id in local_scores]
                if locally_recalled:
                    entries = locally_recalled

            ranked, reranked, degraded_error = _rank_entries(
                entries,
                query_text=query_text,
                terms=terms,
                recall_scores=local_scores,
                reranker=self._reranker if original_count > budget else None,
                rerank_enabled=self._rerank_enabled,
            )
            entities_reranked += int(reranked)
            if degraded_error:
                degraded_errors.add(degraded_error)
            selected = ranked[:budget]
            properties: dict[str, list[SchemaPropertyEntry]] = {}
            for _score, _lexical, name, entry in selected:
                properties.setdefault(name, []).append(entry)
            _sort_timelines(properties)
            entity.properties = properties
            entity.truncated = entity.truncated or len(selected) < original_count
            result.selected[entity.entity_id] = entity.selected_payload(result.mode)

        result.entities = [entity for entity in result.entities if entity.properties]
        retained = {entity.entity_id for entity in result.entities}
        result.selected = {
            entity_id: payload
            for entity_id, payload in result.selected.items()
            if entity_id in retained
        }
        result.entity_scores = {
            entity_id: score
            for entity_id, score in result.entity_scores.items()
            if entity_id in retained
        }
        result.fallback_entity_ids = [
            entity_id for entity_id in result.fallback_entity_ids if entity_id in retained
        ]
        prior = result.shrink_diagnostics or SchemaShrinkDiagnostics()
        result.shrink_diagnostics = SchemaShrinkDiagnostics(
            entries_before=entries_before,
            entries_after=sum(_entry_count(entity) for entity in result.entities),
            entities_reranked=entities_reranked,
            direct_entries_retained=prior.direct_entries_retained,
            timeline_neighbors_added=prior.timeline_neighbors_added,
            degraded_errors=tuple(sorted(degraded_errors)),
        )
        return result

    def restore_direct_hits(
        self,
        result: SchemaTemporalResult,
        full_entities: dict[str, TemporalEntity],
        query: SchemaTemporalQuery,
        *,
        extension_step: int = 3,
    ) -> SchemaTemporalResult:
        """Late-fuse direct Property hits and same-property timeline neighbors.

        ``full_entities`` must be the knowledge-visible view captured before
        shrinking. Event-time selection is reapplied after restoration.
        """

        step = max(0, int(extension_step))
        current = {entity.entity_id: entity for entity in result.entities}
        direct_retained: set[str] = set()
        neighbors_added: set[str] = set()
        for entity_id, unit_ids in result.direct_property_unit_ids.items():
            source = full_entities.get(entity_id)
            if source is None:
                continue
            target = current.get(entity_id)
            if target is None:
                target = _empty_entity(source)
                current[entity_id] = target
            locations = _entry_locations(source)
            ordered_ids = sorted(
                unit_ids,
                key=lambda unit_id: (-result.direct_property_scores.get(unit_id, 0.0), unit_id),
            )
            for unit_id in ordered_ids:
                location = locations.get(unit_id)
                if location is None:
                    continue
                property_name, index = location
                timeline = source.properties[property_name]
                direct = timeline[index]
                target.add(direct)
                direct_retained.add(direct.unit_id)
                if property_name == "default_property" or step == 0:
                    continue
                start = max(0, index - step)
                end = min(len(timeline), index + step + 1)
                for entry in timeline[start:end]:
                    if entry.unit_id == direct.unit_id:
                        continue
                    if _add_if_missing(target, entry):
                        neighbors_added.add(entry.unit_id)

        visible: list[TemporalEntity] = []
        for entity in current.values():
            selected = entity.event_filtered(query)
            if selected.properties:
                visible.append(selected)
        position = {entity.entity_id: index for index, entity in enumerate(result.entities)}
        visible.sort(
            key=lambda entity: (
                position.get(entity.entity_id, len(position)),
                -result.entity_scores.get(entity.entity_id, 0.0),
                entity.entity_id,
            )
        )
        result.entities = visible
        result.selected = {
            entity.entity_id: entity.selected_payload(result.mode) for entity in visible
        }
        retained = {entity.entity_id for entity in visible}
        result.fallback_entity_ids = [
            entity.entity_id for entity in visible if entity.fallback_used
        ]
        result.entity_scores = {
            entity_id: score
            for entity_id, score in result.entity_scores.items()
            if entity_id in retained
        }
        prior = result.shrink_diagnostics or SchemaShrinkDiagnostics()
        visible_ids = {
            entry.unit_id
            for entity in visible
            for timeline in entity.properties.values()
            for entry in timeline
        }
        result.shrink_diagnostics = SchemaShrinkDiagnostics(
            entries_before=prior.entries_before,
            entries_after=sum(_entry_count(entity) for entity in visible),
            entities_reranked=prior.entities_reranked,
            direct_entries_retained=len(direct_retained & visible_ids),
            timeline_neighbors_added=len(neighbors_added & visible_ids),
            degraded_errors=prior.degraded_errors,
        )
        return result

    def _allocate_budgets(self, entry_counts: list[int]) -> list[int]:
        if not entry_counts:
            return []
        minimum = max(1, int(self._max_per_entity * self._allocation_min_factor))
        maximum = max(minimum, int(self._max_per_entity * self._allocation_max_factor))
        total = sum(entry_counts)
        if total <= 0:
            return [self._max_per_entity for _count in entry_counts]
        total_budget = self._max_per_entity * len(entry_counts)
        budgets: list[int] = []
        for count in entry_counts:
            proportional = max(1, int(total_budget * count / total))
            budgets.append(max(minimum, min(maximum, proportional)))
        return budgets


def _query_terms(query: ParsedQuery) -> set[str]:
    values = [*query.tokens, *query.keywords]
    values.extend(re.findall(r"[\w\u4e00-\u9fff]+", query.rewritten or query.raw))
    return {value.casefold() for value in values if value.strip()}


def _flatten(entity: TemporalEntity) -> list[tuple[str, SchemaPropertyEntry]]:
    return [(name, entry) for name, timeline in entity.properties.items() for entry in timeline]


def _rank_entries(
    entries: list[tuple[str, SchemaPropertyEntry]],
    *,
    query_text: str,
    terms: set[str],
    recall_scores: dict[str, float],
    reranker: Reranker | None,
    rerank_enabled: bool,
) -> tuple[list[tuple[float, float, str, SchemaPropertyEntry]], bool, str]:
    texts = [f"{name}: {entry.value}" for name, entry in entries]
    lexical = [_entry_lexical_score(text, terms) for text in texts]
    relevance = [
        recall_scores.get(entry.unit_id, lexical_score)
        for (_name, entry), lexical_score in zip(entries, lexical, strict=True)
    ]
    reranked = False
    degraded_error = ""
    if rerank_enabled and reranker is not None and entries:
        try:
            relevance = [float(score) for score in reranker.rerank(query_text, texts)]
            if len(relevance) != len(entries) or not all(
                math.isfinite(score) for score in relevance
            ):
                raise ValueError("property reranker returned invalid scores")
            reranked = True
        except Exception as exc:
            relevance = list(lexical)
            degraded_error = type(exc).__name__
    ranked = [
        (score, lexical_score, name, entry)
        for (name, entry), score, lexical_score in zip(
            entries,
            relevance,
            lexical,
            strict=True,
        )
    ]
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3].unit_id))
    return ranked, reranked, degraded_error


def _entry_lexical_score(text: str, terms: set[str]) -> float:
    haystack = text.casefold()
    return float(sum(1 for term in terms if term in haystack))


def _entry_count(entity: TemporalEntity) -> int:
    return sum(len(timeline) for timeline in entity.properties.values())


def _sort_timelines(properties: dict[str, list[SchemaPropertyEntry]]) -> None:
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for timeline in properties.values():
        timeline.sort(
            key=lambda entry: (
                entry.event_start or entry.event_time or epoch,
                entry.unit_id,
            )
        )


def _entry_locations(entity: TemporalEntity) -> dict[str, tuple[str, int]]:
    locations: dict[str, tuple[str, int]] = {}
    for property_name, timeline in entity.properties.items():
        for index, entry in enumerate(timeline):
            locations[entry.unit_id] = (property_name, index)
    return locations


def _empty_entity(source: TemporalEntity) -> TemporalEntity:
    return TemporalEntity(
        entity_id=source.entity_id,
        name=source.name,
        entity_type=source.entity_type,
        description=source.description,
        aliases=list(source.aliases),
        search_fields=list(source.search_fields),
        truncated=source.truncated,
    )


def _add_if_missing(entity: TemporalEntity, entry: SchemaPropertyEntry) -> bool:
    timeline = entity.properties.get(entry.property_name, [])
    if any(current.unit_id == entry.unit_id for current in timeline):
        return False
    entity.add(entry)
    return True
