"""Durable canonical entity registry for Schema extraction.

Registry records are derived identity projections stored outside ``/memory/``. Property
MemoryUnits remain the factual truth; the registry can therefore be rebuilt from them.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryTier, MemoryUnit, Segment, Temporal
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.types import Document, VectorRecord
from jiuwen_memory.storage.vector import VectorStore

SCHEMA_ENTITY_KEY_PREFIX = "/schema/entities/"
SCHEMA_ENTITY_VECTOR_MANIFEST_PREFIX = "/schema/entity-vector-manifest/"

logger = get_logger(__name__)


def schema_entity_key(entity_id: str) -> str:
    """Return the hidden KV key for one canonical entity."""

    return f"{SCHEMA_ENTITY_KEY_PREFIX}{entity_id}"


class SchemaEntityRegistry:
    """Merge resolved observations into one derived entity record per identity."""

    def __init__(
        self,
        kv: KVStore,
        *,
        vector_store: VectorStore | None = None,
        fulltext_store: FulltextStore | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._kv = kv
        self._vector = vector_store
        self._fulltext = fulltext_store
        self._embedder = embedder

    def list(self, scope, schema_name: str, *, limit: int = 1000) -> list[MemoryUnit]:
        """Load active canonical entities for one schema and scope."""

        result: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(scope, SCHEMA_ENTITY_KEY_PREFIX)[:limit]:
            entity = loads(raw)
            if entity is None or entity.lifecycle is not LifecycleState.ACTIVE:
                continue
            if str(entity.system_metadata.get("schema_name") or "") == schema_name:
                result.append(entity)
        return result

    def sync(self, observations: list[MemoryUnit]) -> list[MemoryUnit]:
        """Upsert registry records after the corresponding Property write succeeds."""

        grouped: dict[tuple[str, ...], list[MemoryUnit]] = {}
        for unit in observations:
            entity_id = str(unit.system_metadata.get("schema_entity_id") or "").strip()
            if not entity_id:
                continue
            scope = unit.scope
            key = (
                scope.org,
                scope.space,
                scope.user,
                scope.agent,
                scope.session,
                entity_id,
            )
            grouped.setdefault(key, []).append(unit)

        records: list[MemoryUnit] = []
        for key, units in grouped.items():
            entity_id = key[-1]
            existing = self._load(units[0], entity_id)
            record = _entity_record(entity_id, units, existing)
            self._upsert(record)
            self._index(record)
            records.append(record)
        return records

    def rebuild(self, properties: list[MemoryUnit]) -> list[MemoryUnit]:
        """Recreate registry records from already persisted schema properties."""

        return self.sync(properties)

    def _load(self, observation: MemoryUnit, entity_id: str) -> MemoryUnit | None:
        try:
            return loads(self._kv.get(observation.scope, schema_entity_key(entity_id)))
        except NotFoundError:
            return None

    def _upsert(self, entity: MemoryUnit) -> None:
        key = schema_entity_key(entity.id)
        payload = dumps(entity)
        try:
            self._kv.update(entity.scope, key, payload)
        except NotFoundError:
            try:
                self._kv.insert(entity.scope, key, payload)
            except ConflictError:
                self._kv.update(entity.scope, key, payload)

    def _index(self, entity: MemoryUnit) -> None:
        """Project one canonical entity into its independent search ports."""

        text = _entity_search_text(entity)
        metadata = {
            "record_kind": "schema_entity",
            "schema_name": str(entity.system_metadata.get("schema_name") or ""),
            "schema_entity_id": entity.id,
            "schema_entity_name": str(
                entity.system_metadata.get("schema_entity_name") or ""
            ),
            "schema_entity_type": str(
                entity.system_metadata.get("schema_entity_type") or ""
            ),
            "lifecycle": entity.lifecycle.value,
        }
        if self._vector is not None and self._embedder is not None:
            fields = _entity_search_fields(entity)
            texts = [text, *fields]
            vectors = self._embedder.embed(texts)
            if len(vectors) != len(texts):
                raise ValueError("embedder returned a different number of entity vectors")
            records = [
                VectorRecord(
                    entity.id,
                    vectors[0],
                    {
                        **metadata,
                        "entity_vector_owner_id": entity.id,
                        "entity_vector_role": "core",
                        "entity_search_field_index": -1,
                    },
                )
            ]
            for index, field in enumerate(fields):
                records.append(
                    VectorRecord(
                        f"{entity.id}#sf{index}",
                        vectors[index + 1],
                        {
                            **metadata,
                            "entity_vector_owner_id": entity.id,
                            "entity_vector_role": "search_field",
                            "entity_search_field_index": index,
                            "entity_search_field": field,
                        },
                    )
                )
            self._upsert_vectors(entity, records)
        if self._fulltext is not None:
            document = Document(entity.id, text, metadata)
            if self._fulltext.get(entity.scope, [entity.id]):
                self._fulltext.update(entity.scope, [document])
            else:
                self._fulltext.insert(entity.scope, [document])

    def _upsert_vectors(self, entity: MemoryUnit, records: list[VectorRecord]) -> None:
        record_ids = [record.id for record in records]
        previous_ids = self._load_vector_manifest(entity)
        stale_ids = [record_id for record_id in previous_ids if record_id not in record_ids]
        if stale_ids:
            self._vector.delete(entity.scope, stale_ids)
        existing_ids = {record.id for record in self._vector.get(entity.scope, record_ids)}
        updates = [record for record in records if record.id in existing_ids]
        inserts = [record for record in records if record.id not in existing_ids]
        if updates:
            self._vector.update(entity.scope, updates)
        if inserts:
            self._vector.insert(entity.scope, inserts)
        self._upsert_raw(
            entity,
            _vector_manifest_key(entity.id),
            json.dumps(record_ids).encode("utf-8"),
        )

    def _load_vector_manifest(self, entity: MemoryUnit) -> list[str]:
        try:
            raw = self._kv.get(entity.scope, _vector_manifest_key(entity.id))
            value = json.loads(raw.decode("utf-8"))
        except (NotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    def _upsert_raw(self, entity: MemoryUnit, key: str, payload: bytes) -> None:
        try:
            self._kv.update(entity.scope, key, payload)
        except NotFoundError:
            try:
                self._kv.insert(entity.scope, key, payload)
            except ConflictError:
                self._kv.update(entity.scope, key, payload)


def _entity_record(
    entity_id: str,
    observations: list[MemoryUnit],
    existing: MemoryUnit | None,
) -> MemoryUnit:
    representative = observations[0]
    names = _dedupe(
        [
            str(representative.system_metadata.get("schema_entity_name") or ""),
            *(
                alias
                for unit in observations
                for alias in _strings(unit.system_metadata.get("schema_entity_aliases"))
            ),
            *(
                [str(existing.system_metadata.get("schema_entity_name") or "")]
                if existing is not None
                else []
            ),
            *(
                _strings(existing.system_metadata.get("schema_entity_aliases"))
                if existing is not None
                else []
            ),
        ]
    )
    name = names[0] if names else entity_id
    descriptions = _dedupe(
        [
            *(existing.content.splitlines() if existing is not None else []),
            *(
                str(unit.system_metadata.get("schema_entity_description") or "")
                for unit in observations
            ),
        ]
    )
    now = datetime.now(timezone.utc)
    record = copy.deepcopy(existing) if existing is not None else MemoryUnit()
    record.id = entity_id
    record.scope = representative.scope
    record.tier = MemoryTier.SEMANTIC
    record.segments = [Segment(content="\n".join(descriptions[:10]), source=representative.source)]
    record.source_ref = representative.source_ref
    record.provenance = _dedupe(
        [
            *(existing.provenance if existing is not None else []),
            *(source for unit in observations for source in unit.provenance),
        ]
    )
    record.temporal = Temporal(
        t_ingest=existing.temporal.t_ingest if existing is not None else now,
        t_valid=existing.temporal.t_valid if existing is not None else now,
        t_message=max(
            (
                unit.temporal.t_message
                for unit in observations
                if unit.temporal.t_message is not None
            ),
            default=existing.temporal.t_message if existing is not None else None,
        ),
    )
    record.lifecycle = LifecycleState.ACTIVE
    record.system_metadata = {
        "record_kind": "schema_entity",
        "schema_name": str(representative.system_metadata.get("schema_name") or ""),
        "schema_entity_id": entity_id,
        "schema_entity_key": entity_id,
        "schema_entity_name": name,
        "schema_entity_normalized_name": str(
            representative.system_metadata.get("schema_entity_normalized_name") or ""
        ),
        "schema_entity_type": str(
            representative.system_metadata.get("schema_entity_type") or ""
        ),
        "schema_entity_aliases": [value for value in names[1:] if value != name],
        "schema_entity_identity_kind": str(
            representative.system_metadata.get("schema_entity_identity_kind") or ""
        ),
    }
    return record


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _entity_search_fields(unit: MemoryUnit) -> list[str]:
    metadata = unit.system_metadata
    values = [
        str(metadata.get("schema_entity_name") or ""),
        *(_strings(metadata.get("schema_entity_aliases"))),
        str(metadata.get("schema_entity_type") or ""),
        unit.content,
    ]
    return _dedupe(values)


def _entity_search_text(unit: MemoryUnit) -> str:
    return " ".join(_entity_search_fields(unit))


def _vector_manifest_key(entity_id: str) -> str:
    return f"{SCHEMA_ENTITY_VECTOR_MANIFEST_PREFIX}{entity_id}"


def _dedupe(values) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = ["SCHEMA_ENTITY_KEY_PREFIX", "SchemaEntityRegistry", "schema_entity_key"]

