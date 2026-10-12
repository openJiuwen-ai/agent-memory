# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Entity and Property recall used by the schema-temporal selector.

The ordinary retrieval pipeline remains the owner of physical recall.  This
module adds an independent schema-aware Property query through ``DomainStore``
and an Entity path seeded by the final ordinary results plus the dedicated
``schema_entities`` stores.  It never exposes a synthetic entity as a raw
MemoryUnit result.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from jiuwen_memory.common.schema_property import (
    is_schema_property_unit,
    schema_property_entity_id,
)
from jiuwen_memory.common.type_def import (
    ChannelError,
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    ParsedQuery,
    RecallChannel,
    Scope,
    ScoredMemoryUnit,
    and_merge,
    matches_memory_unit,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.types import TextQuery, VectorQuery
from jiuwen_memory.storage.vector import VectorStore

from .model import SchemaTemporalQuery, schema_unit_known_at

_SCHEMA_MODE_FIELD = "system_metadata.extraction_mode"
_ENTITY_ID_FIELDS = (
    "system_metadata.schema_entity_id",
    "system_metadata.schema_entity_key",
)


@dataclass(frozen=True, slots=True)
class SchemaPropertyRecallHit:
    """One real Property MemoryUnit recalled through the schema path."""

    candidate: ScoredMemoryUnit
    entity_id: str
    property_name: str
    channels: tuple[RecallChannel, ...] = ()

    @property
    def unit_id(self) -> str:
        return self.candidate.unit_id

    @property
    def score(self) -> float:
        return self.candidate.score


@dataclass(slots=True)
class SchemaEntityCandidate:
    """Internal entity seed; this object is never returned to API callers."""

    entity_id: str
    score: float
    channels: set[str] = field(default_factory=set)
    direct_property_hits: list[SchemaPropertyRecallHit] = field(default_factory=list)


class SchemaDualPathRecaller:
    """Run independent Entity and Property paths without requiring a graph.

    The Property path executes a schema-filtered keyword/vector recall against
    the same ``DomainStore`` as the ordinary pipeline.  The Entity path consumes
    final ordinary entity seeds plus independent entity fulltext/vector hits.
    Keeping those paths separate prevents an Entity top-N cap from deleting a
    strong direct Property hit.
    """

    def __init__(
        self,
        domain_store: DomainStore,
        *,
        entity_fulltext_store: FulltextStore | None = None,
        entity_vector_store: VectorStore | None = None,
        entity_top_k: int = 20,
        property_top_k: int = 50,
        rrf_k: int = 60,
    ) -> None:
        self._domain = domain_store
        self._entity_fulltext = entity_fulltext_store
        self._entity_vector = entity_vector_store
        self._entity_top_k = max(1, int(entity_top_k))
        self._property_top_k = max(1, int(property_top_k))
        self._rrf_k = max(1, int(rrf_k))
        self.last_errors: list[ChannelError] = []

    def reset_errors(self) -> None:
        """Discard errors collected by the previous top-level search."""

        self.last_errors.clear()

    def recall(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        seed_entity_ids: list[str] | None = None,
        allow_entity_index: bool = True,
        entity_limit: int = 20,
        property_filters: FilterExpr | None = None,
        temporal_query: SchemaTemporalQuery | None = None,
    ) -> list[SchemaEntityCandidate]:
        """Compatibility facade; the searcher uses the two paths independently."""

        temporal_query = temporal_query or SchemaTemporalQuery(
            knowledge_as_of=query.as_of,
            include_archived=query.include_archived,
        )
        entity_candidates = self.recall_entities(
            scope,
            query,
            seed_entity_ids=seed_entity_ids,
            allow_entity_index=allow_entity_index,
            entity_limit=entity_limit,
        )
        property_hits = self.recall_property_hits(
            scope,
            query,
            temporal_query,
            filters=property_filters,
        )
        return _combine_recall_paths(
            entity_candidates,
            property_hits,
            rrf_k=self._rrf_k,
            entity_limit=entity_limit,
        )

    def recall_entities(
        self,
        scope: Scope,
        query: ParsedQuery,
        *,
        seed_entity_ids: list[str] | None = None,
        allow_entity_index: bool = True,
        entity_limit: int,
    ) -> list[SchemaEntityCandidate]:
        """Return the Entity path without mixing in direct Property owners."""

        ranked_sources: list[tuple[str, list[str]]] = []
        seeds = list(dict.fromkeys(seed_entity_ids or []))
        if seeds:
            ranked_sources.append(("ordinary_seed", seeds))
        if allow_entity_index:
            ranked_sources.extend(self._recall_entity_indices(scope, query))
        contributions: dict[str, float] = {}
        channels: dict[str, set[str]] = {}
        for source, entity_ids in ranked_sources:
            for rank, owner_id in enumerate(entity_ids, start=1):
                if not owner_id:
                    continue
                contribution = 1.0 / (self._rrf_k + rank)
                contributions[owner_id] = contributions.get(owner_id, 0.0) + contribution
                channels.setdefault(owner_id, set()).add(source)

        ranked = [
            SchemaEntityCandidate(entity_id, score, channels.get(entity_id, set()))
            for entity_id, score in contributions.items()
        ]
        ranked.sort(key=lambda candidate: (-candidate.score, candidate.entity_id))
        return ranked[: max(1, int(entity_limit))]

    def _recall_entity_indices(
        self,
        scope: Scope,
        query: ParsedQuery,
    ) -> list[tuple[str, list[str]]]:
        """Recall the independent ``schema_entities`` fulltext/vector ports."""

        sources: list[tuple[str, list[str]]] = []
        if self._entity_fulltext is not None and query.raw.strip():
            hits = self._entity_fulltext.search(
                scope,
                TextQuery(
                    text=query.rewritten or query.raw,
                    top_k=self._entity_top_k,
                ),
            )
            if hits:
                sources.append(("entity_keyword", [hit.id for hit in hits]))
        if self._entity_vector is not None and query.vector:
            hits = self._entity_vector.search(
                scope,
                VectorQuery(
                    vector=query.vector,
                    top_k=self._entity_top_k * 5,
                    return_metadata=True,
                ),
            )
            entity_ids = []
            for hit in hits:
                entity_id = _entity_id_from_vector_hit(hit)
                if entity_id and entity_id not in entity_ids:
                    entity_ids.append(entity_id)
                if len(entity_ids) >= self._entity_top_k:
                    break
            if entity_ids:
                sources.append(("entity_vector", entity_ids))
        return sources

    def recall_property_hits(
        self,
        scope: Scope,
        query: ParsedQuery,
        temporal_query: SchemaTemporalQuery,
        *,
        filters: FilterExpr | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[SchemaPropertyRecallHit]:
        """Recall Property units globally or inside one entity.

        ``entity_id`` creates the entity-local second retrieval pass.  The
        predicate is pushed to every configured keyword/vector recaller and is
        checked again on the materialized MemoryUnit.
        """

        recall_limit = max(1, int(limit or self._property_top_k))
        effective_filters = _property_filters(filters, temporal_query, entity_id)
        property_query = replace(
            query,
            scalar_filters=effective_filters,
            recheck_filters=effective_filters,
            as_of=temporal_query.knowledge_as_of,
            time_from=None,
            time_to=None,
            include_archived=temporal_query.include_archived,
        )
        result = self._domain.recall_and_get(
            scope,
            property_query,
            channels=[RecallChannel.KEYWORD, RecallChannel.VECTOR],
            recall_limit=recall_limit,
        )
        self.last_errors.extend(result.errors)
        return _fuse_property_batches(
            result.batches,
            temporal_query,
            effective_filters,
            entity_id=entity_id,
            rrf_k=self._rrf_k,
        )[:recall_limit]


def schema_entity_id(unit: MemoryUnit) -> str:
    """Return a stable property owner, with a legacy identity fallback."""

    return schema_property_entity_id(unit)


def is_schema_property(unit: MemoryUnit) -> bool:
    return is_schema_property_unit(unit)


def _property_filters(
    filters: FilterExpr | None,
    query: SchemaTemporalQuery,
    entity_id: str | None,
) -> FilterExpr | None:
    clauses: list[FilterExpr] = [FilterClause(_SCHEMA_MODE_FIELD, FilterOp.EQ, "schema")]
    if query.knowledge_as_of is not None:
        timestamp = int(query.knowledge_as_of.timestamp() * 1000)
        clauses.extend(
            [
                FilterClause("lifecycle", FilterOp.NE, LifecycleState.FORGOTTEN.value),
                FilterClause("t_valid", FilterOp.LTE, timestamp),
                FilterClause("t_invalid", FilterOp.GT, timestamp),
            ]
        )
    else:
        allowed = [LifecycleState.ACTIVE.value]
        if query.include_archived:
            allowed.extend(
                [LifecycleState.ARCHIVED.value, LifecycleState.SUPERSEDED.value]
            )
        clauses.append(FilterClause("lifecycle", FilterOp.IN, allowed))
    if entity_id:
        identities: list[FilterExpr] = [
            FilterClause(field, FilterOp.EQ, entity_id) for field in _ENTITY_ID_FIELDS
        ]
        if "::" in entity_id:
            entity_type, entity_name = entity_id.split("::", 1)
            identities.append(
                FilterGroup(
                    FilterLogic.AND,
                    [
                        FilterClause(
                            "system_metadata.schema_entity_type",
                            FilterOp.EQ,
                            entity_type,
                        ),
                        FilterClause(
                            "system_metadata.schema_entity_name",
                            FilterOp.EQ,
                            entity_name,
                        ),
                    ],
                )
            )
        clauses.append(FilterGroup(FilterLogic.OR, identities))
    return and_merge(filters, clauses)


def _fuse_property_batches(
    batches,
    temporal_query: SchemaTemporalQuery,
    filters: FilterExpr | None,
    *,
    entity_id: str | None,
    rrf_k: int,
) -> list[SchemaPropertyRecallHit]:
    scores: dict[str, float] = {}
    candidates: dict[str, ScoredMemoryUnit] = {}
    channels: dict[str, set[RecallChannel]] = {}
    for batch in batches:
        for rank, candidate in enumerate(batch.candidates, start=1):
            unit = candidate.unit
            owner_id = schema_entity_id(unit)
            if not is_schema_property(unit) or not _visible(unit, temporal_query, filters):
                continue
            if entity_id is not None and owner_id != entity_id:
                continue
            scores[unit.id] = scores.get(unit.id, 0.0) + 1.0 / (rrf_k + rank)
            candidates[unit.id] = candidate
            channels.setdefault(unit.id, set()).add(batch.channel)

    hits = []
    for unit_id, score in scores.items():
        candidate = candidates[unit_id]
        rescored = replace(candidate, score=score, channel=RecallChannel.TEMPORAL)
        hits.append(
            SchemaPropertyRecallHit(
                candidate=rescored,
                entity_id=schema_entity_id(candidate.unit),
                property_name=str(candidate.unit.system_metadata.get("schema_property_name") or ""),
                channels=tuple(sorted(channels[unit_id], key=lambda value: value.value)),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.unit_id))
    return hits


def _visible(
    unit: MemoryUnit,
    query: SchemaTemporalQuery,
    filters: FilterExpr | None,
) -> bool:
    return schema_unit_known_at(unit, query) and matches_memory_unit(unit, filters)


def _entity_id_from_vector_hit(hit) -> str:
    metadata = hit.metadata if isinstance(hit.metadata, dict) else {}
    owner = metadata.get("entity_vector_owner_id") or metadata.get("schema_entity_id")
    return str(owner or hit.id.split("#sf", 1)[0]).strip()


def _combine_recall_paths(
    entity_candidates: list[SchemaEntityCandidate],
    property_hits: list[SchemaPropertyRecallHit],
    *,
    rrf_k: int,
    entity_limit: int,
) -> list[SchemaEntityCandidate]:
    """Compatibility fusion only; the temporal search path never calls this."""

    property_entity_order = list(
        dict.fromkeys(hit.entity_id for hit in property_hits if hit.entity_id)
    )
    hits_by_entity: dict[str, list[SchemaPropertyRecallHit]] = {}
    for hit in property_hits:
        hits_by_entity.setdefault(hit.entity_id, []).append(hit)

    combined = {
        candidate.entity_id: SchemaEntityCandidate(
            candidate.entity_id,
            candidate.score,
            set(candidate.channels),
            hits_by_entity.get(candidate.entity_id, []),
        )
        for candidate in entity_candidates
    }
    for rank, entity_id in enumerate(property_entity_order, start=1):
        candidate = combined.setdefault(
            entity_id,
            SchemaEntityCandidate(entity_id, 0.0),
        )
        candidate.score += 1.0 / (rrf_k + rank)
        candidate.channels.add("property")
        candidate.direct_property_hits = hits_by_entity.get(entity_id, [])

    candidates = list(combined.values())
    candidates.sort(key=lambda item: (-item.score, item.entity_id))
    return candidates[: max(1, int(entity_limit))]
