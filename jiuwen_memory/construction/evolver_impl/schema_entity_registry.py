"""Persist the hidden Schema Entity Registry independently from graph projection.

This is the entity-only portion of agent-memory's ``SchemaGraphProjector``.  Canonical
entities are durable KV truth under ``/schema/entities/``; optional named vector/fulltext
ports are rebuildable projections.  No graph backend or temporal retrieval is required.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import Document, VectorRecord

logger = get_logger(__name__)

SCHEMA_ENTITY_KEY_PREFIX = "/schema/entities/"
SCHEMA_ENTITY_VECTOR_MANIFEST_PREFIX = "/schema/entity-vector-ids/"


def schema_entity_key(entity_id: str) -> str:
    return f"{SCHEMA_ENTITY_KEY_PREFIX}{entity_id}"


class SchemaEntityRegistry:
    """Upsert canonical entity truth and optional entity search projections."""

    def __init__(self, storage: Storage, embedder: Embedder | None = None) -> None:
        self._storage = storage
        self._kv = storage.kv
        self._embedder = embedder

    def sync(self, observations: list[MemoryUnit]) -> list[MemoryUnit]:
        """Merge resolved observations into one durable record per canonical entity."""

        schema_observations = [unit for unit in observations if _is_schema_observation(unit)]
        if not schema_observations:
            return []
        result: list[MemoryUnit] = []
        for _scope_key, scoped_observations in _group_by_scope(schema_observations):
            result.extend(self._upsert_entities(scoped_observations[0].scope, scoped_observations))
        return result

    def rebuild(self, scope: Scope) -> list[MemoryUnit]:
        """Reconstruct the registry and its optional indexes from property MemoryUnits."""

        properties: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, "/memory/"):
            unit = loads(raw)
            if unit is not None and _is_schema_property(unit):
                properties.append(unit)

        existing: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, SCHEMA_ENTITY_KEY_PREFIX):
            unit = loads(raw)
            if unit is not None:
                existing.append(unit)
        derived = self._upsert_entities(scope, properties)
        by_id = {unit.id: unit for unit in existing}
        by_id.update({unit.id: unit for unit in derived})
        for entity in by_id.values():
            try:
                self._index_entity(scope, entity)
            except Exception as exc:
                logger.warning("SchemaEntityRegistry: entity reindex failed: %s", exc)
        return list(by_id.values())

    def _upsert_entities(
        self,
        scope: Scope,
        observations: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        grouped: dict[str, list[MemoryUnit]] = defaultdict(list)
        for unit in observations:
            entity_id = str(unit.system_metadata.get("schema_entity_id") or "").strip()
            if entity_id:
                grouped[entity_id].append(unit)
        result: list[MemoryUnit] = []
        for entity_id, units in grouped.items():
            key = schema_entity_key(entity_id)
            existing = _load_optional(self._kv, scope, key)
            entity = _entity_unit(entity_id, units, existing)
            _upsert_hidden(self._kv, scope, key, entity)
            try:
                self._index_entity(scope, entity)
            except Exception as exc:
                logger.warning(
                    "SchemaEntityRegistry: entity index update failed; KV truth retained: %s",
                    exc,
                )
            result.append(entity)
        return result

    def _index_entity(self, scope: Scope, entity: MemoryUnit) -> None:
        text = _entity_search_text(entity)
        metadata = entity.system_metadata
        base_metadata = {
            "record_kind": "schema_entity",
            "schema_name": str(metadata.get("schema_name") or ""),
            "schema_entity_id": entity.id,
            "schema_entity_name": str(metadata.get("schema_entity_name") or ""),
            "schema_entity_type": str(metadata.get("schema_entity_type") or ""),
            "lifecycle": entity.lifecycle.value,
        }
        if self._storage.has_vector_port("schema_entities") and self._embedder is not None:
            vector_store = self._storage.vector_port("schema_entities")
            search_fields = _entity_search_fields(entity)
            texts = [text, *search_fields]
            vectors = self._embedder.embed(texts)
            if len(vectors) != len(texts):
                raise ValueError("embedder returned a different number of entity vectors")
            records = [
                VectorRecord(
                    id=entity.id,
                    vector=vectors[0],
                    metadata={
                        **base_metadata,
                        "entity_vector_owner_id": entity.id,
                        "entity_vector_role": "core",
                        "entity_search_field_index": -1,
                    },
                )
            ]
            records.extend(
                VectorRecord(
                    id=f"{entity.id}#sf{index}",
                    vector=vectors[index + 1],
                    metadata={
                        **base_metadata,
                        "entity_vector_owner_id": entity.id,
                        "entity_vector_role": "search_field",
                        "entity_search_field_index": index,
                        "entity_search_field": field,
                    },
                )
                for index, field in enumerate(search_fields)
            )
            record_ids = [record.id for record in records]
            previous_ids = _load_entity_vector_ids(self._kv, scope, entity.id)
            stale_ids = [record_id for record_id in previous_ids if record_id not in record_ids]
            if stale_ids:
                vector_store.delete(scope, stale_ids)
            existing_ids = {record.id for record in vector_store.get(scope, record_ids)}
            updates = [record for record in records if record.id in existing_ids]
            inserts = [record for record in records if record.id not in existing_ids]
            if updates:
                vector_store.update(scope, updates)
            if inserts:
                vector_store.insert(scope, inserts)
            _store_entity_vector_ids(self._kv, scope, entity.id, record_ids)
        if self._storage.has_fulltext_port("schema_entities"):
            fulltext = self._storage.fulltext_port("schema_entities")
            document = Document(id=entity.id, text=text, metadata=base_metadata)
            if fulltext.get(scope, [entity.id]):
                fulltext.update(scope, [document])
            else:
                fulltext.insert(scope, [document])


def _entity_unit(
    entity_id: str,
    observations: list[MemoryUnit],
    existing: MemoryUnit | None,
) -> MemoryUnit:
    representative = observations[0]
    names = _dedupe(
        [
            str(representative.system_metadata.get("schema_entity_name") or ""),
            *(
                str(name)
                for unit in observations
                for name in unit.system_metadata.get("schema_entity_aliases", [])
            ),
            *(
                [str(existing.system_metadata.get("schema_entity_name") or "")]
                if existing is not None
                else []
            ),
            *(
                existing.system_metadata.get("schema_entity_aliases", [])
                if existing is not None
                else []
            ),
        ]
    )
    name = names[0] if names else entity_id
    aliases = [value for value in names[1:] if value != name]
    descriptions = _dedupe(
        [
            *(existing.content.split("\n") if existing is not None else []),
            *(
                str(unit.system_metadata.get("schema_entity_description") or "")
                for unit in observations
                if unit.system_metadata.get("schema_entity_description")
            ),
        ]
    )
    if not descriptions:
        descriptions = _dedupe(unit.content for unit in observations if unit.content)
    now = datetime.now(timezone.utc)
    return MemoryUnit(
        id=entity_id,
        scope=representative.scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content="\n".join(descriptions[:10]), source=representative.source)],
        source_ref=representative.source_ref,
        temporal=Temporal(
            t_ingest=existing.temporal.t_ingest if existing is not None else now,
            t_valid=existing.temporal.t_valid if existing is not None else now,
            t_message=max(
                (unit.temporal.t_message for unit in observations if unit.temporal.t_message),
                default=existing.temporal.t_message if existing is not None else None,
            ),
        ),
        provenance=_dedupe(
            [
                *(existing.provenance if existing is not None else []),
                *(
                    source
                    for unit in observations
                    for source in [unit.source_ref, *unit.provenance]
                ),
            ]
        ),
        tags=["schema", "schema_entity"],
        system_metadata={
            "record_kind": "schema_entity",
            "schema_name": str(representative.system_metadata.get("schema_name") or ""),
            "schema_version": str(representative.system_metadata.get("schema_version") or ""),
            "schema_entity_id": entity_id,
            "schema_entity_name": name,
            "schema_entity_normalized_name": _normalize_name(name),
            "schema_entity_type": str(
                representative.system_metadata.get("schema_entity_type") or ""
            ),
            "schema_entity_aliases": aliases,
            "schema_entity_identity_kind": next(
                (
                    str(unit.system_metadata.get("schema_entity_identity_kind") or "")
                    for unit in observations
                    if unit.system_metadata.get("schema_entity_identity_kind")
                ),
                str(
                    existing.system_metadata.get("schema_entity_identity_kind") or ""
                    if existing is not None
                    else ""
                ),
            ),
        },
        lifecycle=LifecycleState.ACTIVE,
        entities=[name],
    )


def _entity_search_fields(unit: MemoryUnit) -> list[str]:
    metadata = unit.system_metadata
    values = [
        str(metadata.get("schema_entity_name") or ""),
        *(str(value) for value in metadata.get("schema_entity_aliases", [])),
        str(metadata.get("schema_entity_type") or ""),
        unit.content,
    ]
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _entity_vector_manifest_key(entity_id: str) -> str:
    return f"{SCHEMA_ENTITY_VECTOR_MANIFEST_PREFIX}{entity_id}"


def _load_entity_vector_ids(kv, scope: Scope, entity_id: str) -> list[str]:
    try:
        value = json.loads(kv.get(scope, _entity_vector_manifest_key(entity_id)).decode("utf-8"))
    except (NotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _store_entity_vector_ids(kv, scope: Scope, entity_id: str, record_ids: list[str]) -> None:
    raw = json.dumps(record_ids, ensure_ascii=False).encode("utf-8")
    _upsert_raw(kv, scope, _entity_vector_manifest_key(entity_id), raw)


def _upsert_hidden(kv, scope: Scope, key: str, unit: MemoryUnit) -> None:
    _upsert_raw(kv, scope, key, dumps(unit))


def _upsert_raw(kv, scope: Scope, key: str, raw: bytes) -> None:
    try:
        kv.insert(scope, key, raw)
    except ConflictError:
        kv.update(scope, key, raw)


def _load_optional(kv, scope: Scope, key: str) -> MemoryUnit | None:
    try:
        return loads(kv.get(scope, key))
    except NotFoundError:
        return None


def _group_by_scope(
    units: list[MemoryUnit],
) -> list[tuple[tuple[str, str, str, str, str], list[MemoryUnit]]]:
    grouped: dict[tuple[str, str, str, str, str], list[MemoryUnit]] = defaultdict(list)
    for unit in units:
        grouped[_scope_tuple(unit.scope)].append(unit)
    return list(grouped.items())


def _scope_tuple(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _is_schema_property(unit: MemoryUnit) -> bool:
    return (
        unit.system_metadata.get("extraction_mode") == "schema"
        and str(unit.system_metadata.get("schema_property_operation") or "set").lower()
        != "delete"
    )


def _is_schema_observation(unit: MemoryUnit) -> bool:
    mode = unit.system_metadata.get("extraction_mode")
    return mode == "schema_entity_observation" or (
        mode == "schema"
        and str(unit.system_metadata.get("schema_property_operation") or "set").lower()
        != "delete"
    )


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _entity_search_text(unit: MemoryUnit) -> str:
    return " ".join(_entity_search_fields(unit))


def _dedupe(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
