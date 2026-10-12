# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Schema-temporal search orchestration over real MemoryUnits."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import replace
from datetime import datetime

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.type_def import (
    FilterExpr,
    MemoryUnit,
    ParsedQuery,
    Scope,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.vector import VectorStore

from .formatter import SchemaTemporalFormatter
from .model import (
    SchemaShrinkDiagnostics,
    SchemaTemporalQuery,
    SchemaTemporalResult,
    TemporalEntity,
)
from .query import resolve_temporal_query
from .reader import PropertyIdLookup, SchemaTemporalReader
from .recall import SchemaDualPathRecaller, SchemaPropertyRecallHit
from .shrink import SchemaTemporalShrinker

logger = get_logger(__name__)


class SchemaTemporalSearcher:
    """Recall, hydrate, shrink and time-filter schema entity timelines.

    Entity and Property paths are intentionally independent.  Direct Property
    hits are fused back *after* the Entity path is shrunk, and then each hit is
    expanded with neighbouring facts from the same property timeline.
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
    ) -> None:
        self._reader = SchemaTemporalReader(
            domain_store,
            property_id_lookup=property_id_lookup,
        )
        self._recaller = SchemaDualPathRecaller(
            domain_store,
            entity_fulltext_store=entity_fulltext_store,
            entity_vector_store=entity_vector_store,
            entity_top_k=entity_top_k,
            property_top_k=property_top_k,
            rrf_k=rrf_k,
        )
        self._auto_enabled = bool(auto_enabled)
        self._property_top_n = max(1, int(property_top_n))
        self._extension_step = max(0, int(property_extension_step))
        self._reranker = property_reranker
        self._shrink = (
            SchemaTemporalShrinker(
                max_properties_per_entity=max_properties_per_entity,
                allocation_min_factor=property_allocation_min_factor,
                allocation_max_factor=property_allocation_max_factor,
                reranker=property_reranker,
                rerank_enabled=property_rerank_enabled,
            )
            if shrink_enabled
            else None
        )
        self._direct_property_rerank_enabled = bool(direct_property_rerank_enabled)
        self._entity_rerank_enabled = bool(entity_rerank_enabled)
        self._entity_rerank_max_chars = max(1, int(entity_rerank_max_chars))

    @property
    def reader(self) -> SchemaTemporalReader:
        return self._reader

    @property
    def recall_errors(self):
        return list(self._recaller.last_errors)

    def should_search(
        self,
        parsed: ParsedQuery,
        explicit: SchemaTemporalQuery | None,
    ) -> bool:
        plan = resolve_temporal_query(explicit, parsed, auto_enabled=self._auto_enabled)
        return explicit is not None or plan.enabled

    def search(
        self,
        scope: Scope,
        parsed: ParsedQuery,
        explicit: SchemaTemporalQuery | None,
        *,
        seed_entity_ids: list[str] | None = None,
        filters: FilterExpr | None = None,
    ) -> SchemaTemporalResult | None:
        self._recaller.reset_errors()
        plan = resolve_temporal_query(explicit, parsed, auto_enabled=self._auto_enabled)
        if explicit is None and not plan.enabled:
            return None

        entities = self._recaller.recall_entities(
            scope,
            parsed,
            seed_entity_ids=seed_entity_ids,
            allow_entity_index=filters is None,
            entity_limit=plan.query.entity_limit,
        )
        property_hits = self._recaller.recall_property_hits(
            scope,
            parsed,
            plan.query,
            filters=filters,
        )
        property_hits, direct_reranked, direct_error = self._rerank_direct_property_hits(
            scope,
            parsed,
            property_hits,
        )
        property_hits = property_hits[: self._property_top_n]
        entity_ids = [candidate.entity_id for candidate in entities]
        result = self._reader.read(
            scope,
            entity_ids,
            plan.query,
            filters=filters,
            apply_event_filter=False,
        )
        result.entity_scores = {candidate.entity_id: candidate.score for candidate in entities}

        # Shrinker deliberately mutates the entity view in place.  Preserve a
        # separate full timeline so direct Property hits and their neighbours
        # can be restored after shrinking.
        full_entities = {entity.entity_id: copy.deepcopy(entity) for entity in result.entities}
        if self._shrink is not None:
            result = self._shrink.shrink(
                result,
                parsed,
                entity_local_recall=lambda entity_id: self._recaller.recall_property_hits(
                    scope,
                    parsed,
                    plan.query,
                    filters=filters,
                    entity_id=entity_id,
                ),
            )

        direct_ids, direct_entity_scores = _group_property_hits(property_hits)
        direct_scores = {hit.unit_id: hit.score for hit in property_hits}
        missing_owner_ids = [
            entity_id for entity_id in direct_ids if entity_id not in full_entities
        ]
        if missing_owner_ids:
            direct_entities = self._reader.read(
                scope,
                missing_owner_ids,
                replace(
                    plan.query,
                    entity_limit=max(plan.query.entity_limit, len(missing_owner_ids)),
                ),
                filters=filters,
                apply_event_filter=False,
            )
            full_entities.update({entity.entity_id: entity for entity in direct_entities.entities})

        _merge_direct_properties(
            result,
            full_entities,
            direct_ids,
            direct_scores,
            direct_entity_scores,
        )
        neighbor_ids = _extend_same_property_timelines(
            result,
            full_entities,
            extension_step=self._extension_step,
        )
        result = _apply_event_filter(result, plan.query)
        _synchronize_direct_diagnostics(result, direct_ids, direct_scores, neighbor_ids)
        final_reranked, final_error = self._rerank_final_entities(result, parsed)
        _record_rerank_diagnostics(
            result,
            direct_properties_reranked=direct_reranked,
            final_entities_reranked=final_reranked,
            errors=(direct_error, final_error),
        )
        return result

    def _rerank_direct_property_hits(
        self,
        scope: Scope,
        parsed: ParsedQuery,
        hits: list[SchemaPropertyRecallHit],
    ) -> tuple[list[SchemaPropertyRecallHit], int, str]:
        if not self._direct_property_rerank_enabled or self._reranker is None or not hits:
            return hits, 0, ""
        try:
            units = self._reader.load_units(scope, [hit.unit_id for hit in hits])
            rerankable = [hit for hit in hits if hit.unit_id in units]
            if not rerankable:
                return hits, 0, ""
            texts = [_property_rerank_document(units[hit.unit_id]) for hit in rerankable]
            scores = _validated_rerank_scores(
                self._reranker,
                parsed.rewritten or parsed.raw,
                texts,
                expected=len(rerankable),
                label="direct property",
            )
        except Exception as exc:
            logger.warning(
                "SchemaTemporalSearcher: direct Property rerank degraded: %s",
                type(exc).__name__,
            )
            return hits, 0, type(exc).__name__
        rescored = {
            hit.unit_id: replace(hit, score=score)
            for hit, score in zip(rerankable, scores, strict=True)
        }
        prior = {hit.unit_id: hit.score for hit in hits}
        ranked = [rescored.get(hit.unit_id, hit) for hit in hits]
        ranked.sort(key=lambda hit: (-hit.score, -prior.get(hit.unit_id, 0.0), hit.unit_id))
        return ranked, len(rerankable), ""

    def _rerank_final_entities(
        self,
        result: SchemaTemporalResult,
        parsed: ParsedQuery,
    ) -> tuple[int, str]:
        if not self._entity_rerank_enabled or self._reranker is None or not result.entities:
            return 0, ""
        formatter = SchemaTemporalFormatter()
        texts = [
            formatter.format_entity(entity)[: self._entity_rerank_max_chars]
            for entity in result.entities
        ]
        try:
            scores = _validated_rerank_scores(
                self._reranker,
                parsed.rewritten or parsed.raw,
                texts,
                expected=len(result.entities),
                label="final entity",
            )
        except Exception as exc:
            logger.warning(
                "SchemaTemporalSearcher: final Entity rerank degraded: %s",
                type(exc).__name__,
            )
            return 0, type(exc).__name__
        prior = dict(result.entity_scores)
        result.entity_scores = {
            entity.entity_id: score
            for entity, score in zip(result.entities, scores, strict=True)
        }
        result.entities.sort(
            key=lambda entity: (
                -result.entity_scores.get(entity.entity_id, 0.0),
                -prior.get(entity.entity_id, 0.0),
                entity.entity_id,
            )
        )
        result.selected = {
            entity.entity_id: entity.selected_payload(result.mode)
            for entity in result.entities
        }
        return len(result.entities), ""


def _validated_rerank_scores(
    reranker: Reranker,
    query: str,
    texts: list[str],
    *,
    expected: int,
    label: str,
) -> list[float]:
    scores = [float(score) for score in reranker.rerank(query, texts)]
    if len(scores) != expected or not all(math.isfinite(score) for score in scores):
        raise ValueError(f"{label} reranker returned invalid scores")
    return scores


def _property_rerank_document(unit: MemoryUnit) -> str:
    metadata = unit.system_metadata
    fields = [
        f"Entity: {metadata.get('schema_entity_name') or metadata.get('schema_entity_id') or ''}",
        f"Type: {metadata.get('schema_entity_type') or ''}",
        f"Property: {metadata.get('schema_property_name') or ''}",
        f"Value: {unit.content}",
    ]
    event_time = _metadata_datetime(metadata.get("schema_event_start"))
    event_time = event_time or unit.temporal.t_event
    if event_time is not None:
        fields.append(f"Event time: {event_time.isoformat()}")
    elif unit.temporal.t_message is not None:
        fields.append(f"Source message time: {unit.temporal.t_message.isoformat()}")
    return "\n".join(fields)


def _metadata_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _record_rerank_diagnostics(
    result: SchemaTemporalResult,
    *,
    direct_properties_reranked: int,
    final_entities_reranked: int,
    errors: tuple[str, ...],
) -> None:
    diagnostics = result.shrink_diagnostics or SchemaShrinkDiagnostics()
    degraded_errors = set(diagnostics.degraded_errors)
    degraded_errors.update(error for error in errors if error)
    result.shrink_diagnostics = replace(
        diagnostics,
        direct_properties_reranked=direct_properties_reranked,
        final_entities_reranked=final_entities_reranked,
        degraded_errors=tuple(sorted(degraded_errors)),
    )


def _group_property_hits(
    hits: list[SchemaPropertyRecallHit],
) -> tuple[dict[str, list[str]], dict[str, float]]:
    unit_ids: dict[str, list[str]] = {}
    score_lists: dict[str, list[float]] = {}
    for hit in hits:
        unit_ids.setdefault(hit.entity_id, []).append(hit.unit_id)
        score_lists.setdefault(hit.entity_id, []).append(hit.score)
    scores = {
        entity_id: sum(values) / len(values) for entity_id, values in score_lists.items() if values
    }
    ordered = sorted(unit_ids, key=lambda entity_id: (-scores.get(entity_id, 0.0), entity_id))
    return {entity_id: unit_ids[entity_id] for entity_id in ordered}, scores


def _merge_direct_properties(
    result: SchemaTemporalResult,
    full_entities: dict[str, TemporalEntity],
    direct_ids: dict[str, list[str]],
    direct_scores: dict[str, float],
    entity_scores: dict[str, float],
) -> None:
    targets = {entity.entity_id: entity for entity in result.entities}
    for entity_id, unit_ids in direct_ids.items():
        source = full_entities.get(entity_id)
        if source is None:
            continue
        target = targets.get(entity_id)
        if target is None:
            target = _empty_entity(source)
            result.entities.append(target)
            targets[entity_id] = target
        locations = _entry_locations(source)
        for unit_id in sorted(
            dict.fromkeys(unit_ids),
            key=lambda value: (-direct_scores.get(value, 0.0), value),
        ):
            location = locations.get(unit_id)
            if location is None:
                continue
            property_name, index = location
            entry = source.properties[property_name][index]
            if not _contains_unit(target, unit_id):
                target.add(entry)
        result.entity_scores.setdefault(entity_id, entity_scores.get(entity_id, 0.0))


def _extend_same_property_timelines(
    result: SchemaTemporalResult,
    full_entities: dict[str, TemporalEntity],
    *,
    extension_step: int,
) -> set[str]:
    if extension_step <= 0:
        return set()
    added: set[str] = set()
    for target in result.entities:
        source = full_entities.get(target.entity_id)
        if source is None:
            continue
        locations = _entry_locations(source)
        disclosed = {
            property_name: list(timeline) for property_name, timeline in target.properties.items()
        }
        for property_name, entries in disclosed.items():
            if property_name == "default_property":
                continue
            full_timeline = source.properties.get(property_name, [])
            for entry in entries:
                location = locations.get(entry.unit_id)
                if location is None or location[0] != property_name:
                    continue
                index = location[1]
                lower = max(0, index - extension_step)
                upper = min(len(full_timeline), index + extension_step + 1)
                for neighbour in full_timeline[lower:upper]:
                    if _contains_unit(target, neighbour.unit_id):
                        continue
                    target.add(neighbour)
                    added.add(neighbour.unit_id)
    return added


def _apply_event_filter(
    result: SchemaTemporalResult,
    query: SchemaTemporalQuery,
) -> SchemaTemporalResult:
    entities = [entity.event_filtered(query) for entity in result.entities]
    result.entities = [entity for entity in entities if entity.properties]
    retained = {entity.entity_id for entity in result.entities}
    result.selected = {
        entity.entity_id: entity.selected_payload(query.mode) for entity in result.entities
    }
    result.entity_scores = {
        entity_id: score
        for entity_id, score in result.entity_scores.items()
        if entity_id in retained
    }
    result.fallback_entity_ids = [
        entity.entity_id for entity in result.entities if entity.fallback_used
    ]
    return result


def _synchronize_direct_diagnostics(
    result: SchemaTemporalResult,
    direct_ids: dict[str, list[str]],
    direct_scores: dict[str, float],
    neighbor_ids: set[str],
) -> None:
    visible_by_entity = {
        entity.entity_id: {
            entry.unit_id for timeline in entity.properties.values() for entry in timeline
        }
        for entity in result.entities
    }
    result.direct_property_unit_ids = {
        entity_id: [
            unit_id for unit_id in unit_ids if unit_id in visible_by_entity.get(entity_id, set())
        ]
        for entity_id, unit_ids in direct_ids.items()
        if any(unit_id in visible_by_entity.get(entity_id, set()) for unit_id in unit_ids)
    }
    visible_direct = {
        unit_id for unit_ids in result.direct_property_unit_ids.values() for unit_id in unit_ids
    }
    result.direct_property_scores = {
        unit_id: direct_scores[unit_id] for unit_id in visible_direct if unit_id in direct_scores
    }
    diagnostics = result.shrink_diagnostics or SchemaShrinkDiagnostics()
    visible = {unit_id for unit_ids in visible_by_entity.values() for unit_id in unit_ids}
    result.shrink_diagnostics = replace(
        diagnostics,
        entries_after=len(visible),
        direct_entries_retained=len(visible_direct),
        timeline_neighbors_added=len(neighbor_ids & visible),
    )


def _entry_locations(entity: TemporalEntity) -> dict[str, tuple[str, int]]:
    return {
        entry.unit_id: (property_name, index)
        for property_name, timeline in entity.properties.items()
        for index, entry in enumerate(timeline)
    }


def _contains_unit(entity: TemporalEntity, unit_id: str) -> bool:
    return any(
        entry.unit_id == unit_id for timeline in entity.properties.values() for entry in timeline
    )


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


def lexical_property_score(query: ParsedQuery, property_name: str, value: str) -> float:
    """Small deterministic fallback used when no model reranker is configured."""

    terms = {token.casefold() for token in [*query.tokens, *query.keywords] if token.strip()}
    terms.update(
        token.casefold()
        for token in re.findall(r"[\w\u4e00-\u9fff]+", query.rewritten or query.raw)
    )
    haystack = f"{property_name} {value}".casefold()
    return float(sum(term in haystack for term in terms))
