# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Hydrate schema property timelines from the MemoryUnit source of truth."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
    MemoryUnit,
    Scope,
    and_merge,
    matches_memory_unit,
)
from jiuwen_memory.storage.domain_store import DomainStore

from .assembler import TemporalEntityAssembler
from .model import SchemaTemporalQuery, SchemaTemporalResult, schema_unit_known_at
from .recall import is_schema_property, schema_entity_id


class PropertyIdLookup(Protocol):
    """Minimal reverse-index lookup accepted by the temporal reader."""

    def __call__(self, scope: Scope, entity_id: str) -> Any:
        ...


class PropertyIdLookupBatch(Protocol):
    """Batch reverse-index lookup accepted by the temporal reader."""

    def __call__(self, scope: Scope, entity_ids: list[str]) -> dict[str, Any]:
        ...


class SchemaTemporalReader:
    """Load complete property histories without creating another truth source.

    A caller may inject an Entity-to-Property id lookup maintained by the write
    path.  When it is unavailable, ``DomainStore.list`` remains the compatible
    fallback.  In either case every id is materialized as a real MemoryUnit.
    """

    def __init__(
        self,
        domain_store: DomainStore,
        *,
        property_id_lookup: PropertyIdLookup | None = None,
        property_id_lookup_batch: PropertyIdLookupBatch | None = None,
    ) -> None:
        self._domain = domain_store
        self._assembler = TemporalEntityAssembler()
        self._property_id_lookup = property_id_lookup
        self._property_id_lookup_batch = property_id_lookup_batch

    def read(
        self,
        scope: Scope,
        entity_ids: list[str],
        query: SchemaTemporalQuery,
        *,
        filters: FilterExpr | None = None,
        apply_event_filter: bool = True,
    ) -> SchemaTemporalResult:
        unique: list[str] = []
        for raw in entity_ids:
            entity_id = str(raw).strip()
            if entity_id and entity_id not in unique:
                unique.append(entity_id)
        entities = self._read_entities(scope, unique[: query.entity_limit], query, filters)
        if apply_event_filter:
            entities = [entity.event_filtered(query) for entity in entities]
        else:
            entities = [entity.knowledge_visible(query) for entity in entities]
        entities = [entity for entity in entities if entity.properties]
        return SchemaTemporalResult(
            mode=query.mode,
            entities=entities,
            selected={entity.entity_id: entity.selected_payload(query.mode) for entity in entities},
            fallback_entity_ids=[entity.entity_id for entity in entities if entity.fallback_used],
        )

    def load_units(self, scope: Scope, unit_ids: list[str]) -> dict[str, MemoryUnit]:
        """Point-read real MemoryUnits while preserving first-id order."""

        unique_ids = list(dict.fromkeys(unit_id for unit_id in unit_ids if unit_id))
        return {unit.id: unit for unit in self._domain.get(scope, unique_ids)}

    def load_provenance_sources(
        self,
        scope: Scope,
        properties: list[MemoryUnit],
    ) -> dict[str, MemoryUnit]:
        source_ids = []
        seen = set()
        for unit in properties:
            for source_id in unit.provenance:
                if not source_id or source_id in seen:
                    continue
                seen.add(source_id)
                source_ids.append(source_id)
        return self.load_units(scope, source_ids)

    def _read_entities(
        self,
        scope: Scope,
        entity_ids: list[str],
        query: SchemaTemporalQuery,
        filters: FilterExpr | None,
    ):
        batch_lookup = self._property_id_lookup_batch or getattr(
            self._property_id_lookup,
            "lookup_batch",
            None,
        )
        if batch_lookup is None or len(entity_ids) <= 1:
            # 单实体走窄前缀 lookup；批量才走 lookup_batch 全 Scope 扫描。
            return [
                self._read_entity(scope, entity_id, query, filters)
                for entity_id in entity_ids
            ]
        lookups = dict(batch_lookup(scope, list(entity_ids)) or {})
        indexed_ids: dict[str, list[str]] = {}
        all_unit_ids: list[str] = []
        for entity_id in entity_ids:
            lookup = lookups.get(entity_id)
            if lookup is None or (hasattr(lookup, "indexed") and not bool(lookup.indexed)):
                continue
            # getattr 兜底 plain-list 返回形态（与 _read_indexed 同口径）。
            unit_ids = list(getattr(lookup, "unit_ids", lookup) or [])
            if not unit_ids:
                # indexed=True 但无 membership：留给下方 unit_ids is None 分支回退 list。
                continue
            indexed_ids[entity_id] = unit_ids
            all_unit_ids.extend(unit_ids)
        units = self.load_units(scope, all_unit_ids) if all_unit_ids else {}
        entities = []
        for entity_id in entity_ids:
            unit_ids = indexed_ids.get(entity_id)
            if unit_ids is None:
                listed, total = self._list_properties(scope, entity_id, query, filters)
                entities.append(self._assemble(entity_id, listed, total, query))
                continue
            matched = self._matching_units(entity_id, unit_ids, units, query, filters)
            entities.append(self._assemble(entity_id, matched, len(matched), query))
        return entities

    def _read_entity(
        self,
        scope: Scope,
        entity_id: str,
        query: SchemaTemporalQuery,
        filters: FilterExpr | None,
    ):
        indexed = self._read_indexed(scope, entity_id, query, filters)
        if indexed is not None:
            units, total = indexed
        else:
            units, total = self._list_properties(scope, entity_id, query, filters)
        return self._assemble(entity_id, units, total, query)

    def _assemble(
        self,
        entity_id: str,
        units: list[MemoryUnit],
        total: int,
        query: SchemaTemporalQuery,
    ):
        return self._assembler.assemble(
            entity_id,
            units[: query.per_entity_limit],
            truncated=total > query.per_entity_limit,
        )

    def _read_indexed(
        self,
        scope: Scope,
        entity_id: str,
        query: SchemaTemporalQuery,
        filters: FilterExpr | None,
    ) -> tuple[list[MemoryUnit], int] | None:
        if self._property_id_lookup is None:
            return None
        lookup = self._property_id_lookup(scope, entity_id)
        if hasattr(lookup, "indexed") and not bool(lookup.indexed):
            return None
        unit_ids = list(getattr(lookup, "unit_ids", lookup or []))
        if not unit_ids:
            # indexed=True 但无 membership：可能是并发写期间 membership 被删但
            # watermark 仍 complete 的窗口。回退 list 真源复核，避免漏召。
            return None
        units = self.load_units(scope, unit_ids)
        matched = self._matching_units(entity_id, unit_ids, units, query, filters)
        return matched, len(matched)

    def _matching_units(
        self,
        entity_id: str,
        unit_ids: list[str],
        units: dict[str, MemoryUnit],
        query: SchemaTemporalQuery,
        filters: FilterExpr | None,
    ) -> list[MemoryUnit]:
        matched = []
        for unit_id in dict.fromkeys(unit_ids):
            unit = units.get(unit_id)
            if unit is None:
                continue
            if schema_entity_id(unit) != entity_id or not is_schema_property(unit):
                continue
            if matches_memory_unit(unit, filters) and _knowledge_visible(unit, query):
                matched.append(unit)
        return _sort_units(matched)

    def _list_properties(
        self,
        scope: Scope,
        entity_id: str,
        query: SchemaTemporalQuery,
        filters: FilterExpr | None,
    ) -> tuple[list[MemoryUnit], int]:
        identity_filter = _entity_filter(entity_id)
        effective = and_merge(
            filters,
            [
                FilterClause(
                    "system_metadata.extraction_mode",
                    FilterOp.EQ,
                    "schema",
                ),
                identity_filter,
            ],
        )
        limit = max(1, query.per_entity_limit)
        result = self._domain.list(scope, offset=0, limit=limit, filters=effective)
        matched = []
        for unit in result.items:
            if schema_entity_id(unit) != entity_id or not is_schema_property(unit):
                continue
            if _knowledge_visible(unit, query):
                matched.append(unit)
        return _sort_units(matched), result.count


def _entity_filter(entity_id: str) -> FilterExpr:
    identities: list[FilterExpr] = [
        FilterClause("system_metadata.schema_entity_id", FilterOp.EQ, entity_id),
        FilterClause("system_metadata.schema_entity_key", FilterOp.EQ, entity_id),
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
    return FilterGroup(FilterLogic.OR, identities)


def _knowledge_visible(unit: MemoryUnit, query: SchemaTemporalQuery) -> bool:
    return schema_unit_known_at(unit, query)


def _sort_units(units: list[MemoryUnit]) -> list[MemoryUnit]:
    epoch = datetime.min.replace(tzinfo=UTC)

    def _anchor(unit: MemoryUnit) -> datetime:
        value = unit.temporal.t_event or unit.temporal.t_message or unit.temporal.t_ingest or epoch
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    return sorted(
        units,
        key=lambda unit: (_anchor(unit), unit.id),
        reverse=True,
    )
