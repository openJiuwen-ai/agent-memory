"""Reuse scoped index vectors; never re-embed a node or alter truth fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
import re

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.index_builder_impl._index_ops import (
    chunk_tracking_key,
    layer_record_id,
)
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.vector import VectorStore

_NAMES = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:[ -](?:[A-Z][a-z]+|[A-Z]{2,})){0,2}\b")
_NON_NAMES = frozenset((
    "I A An The This That These Those He She They It We You My His Her Their Our "
    "User Assistant Session Summary Date Time From To On In At By And But With "
    "January February March April May June July August September October November December "
    "Monday Tuesday Wednesday Thursday Friday Saturday Sunday AM PM UTC "
    "Goal Actions Outcome Unknown Unspecified"
).casefold().split())


def surface_entities(text: str) -> set[str]:
    """Conservative proper-name hints, not typed NER or fact extraction."""
    return {name.casefold() for name in _NAMES.findall(text)
            if any(word.casefold() not in _NON_NAMES for word in name.split())}


def mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    if not dim or any(len(v) != dim or any(
        type(x) not in (int, float) or not math.isfinite(x) for x in v
    ) for v in vectors):
        return []
    normalized = []
    for v in vectors:
        norm = math.hypot(*v)
        if norm == 0 or not math.isfinite(norm):
            return []
        normalized.append([x / norm for x in v])
    result = [sum(column) / len(normalized) for column in zip(*normalized, strict=True)]
    norm = math.hypot(*result)
    return [x / norm for x in result] if norm else []


class IndexedGraphVectors:
    """Read only known IDs through authorized, same-profile store ports.

    Exact text hashes reject stale projections. Legacy indexes without hashes must
    be refreshed via their existing IndexBuilder, not silently trusted or re-embedded here.
    """

    _CHUNK_KEY_BATCH_SIZE = 64

    def __init__(self, vector: VectorStore | None, layers_l1: VectorStore | None,
                 kv: KVStore | None, *, prefer_l1: bool = False) -> None:
        self._vector, self._layers_l1, self._kv = vector, layers_l1, kv
        self._prefer_l1 = prefer_l1

    @staticmethod
    def _valid(record, unit: MemoryUnit, text: str) -> bool:
        return (record.metadata.get("unit_id") == unit.id
                and record.metadata.get("indexed_text_sha256")
                == hashlib.sha256(text.encode()).hexdigest())

    def __call__(self, unit: MemoryUnit) -> list[float]:
        if self._prefer_l1:
            vector = self._l1_vector(unit)
            if vector:
                return vector
        # L2 covers the detailed body. Normalize each existing chunk before pooling.
        if self._vector is not None and self._kv is not None:
            try:
                raw = self._kv.get(unit.scope, chunk_tracking_key(unit.id))
            except NotFoundError:
                raw = None
            vector = self._detailed_vector(unit, raw)
            if vector:
                return vector
        return self._l1_vector(unit)

    def read_many(self, units: list[MemoryUnit]) -> dict[str, list[float]]:
        """Batch chunk-key metadata for one exact Scope; validate each vector as before."""
        if not units:
            return {}
        scope = units[0].scope
        if any(u.scope != scope for u in units):
            raise ValidationError("indexed graph vectors require one exact Scope")
        if self._prefer_l1 or self._vector is None or self._kv is None:
            return {unit.id: self(unit) for unit in units}
        result = {}
        size = self._CHUNK_KEY_BATCH_SIZE
        for start in range(0, len(units), size):
            group = units[start:start + size]
            if len(group) == 1:
                result[group[0].id] = self(group[0])
                continue
            try:
                chunks = self._kv.mget(scope, [chunk_tracking_key(unit.id) for unit in group])
            except NotFoundError:
                # mget fails as a unit when a legacy node has no tracking key.
                result.update((unit.id, self(unit)) for unit in group)
            else:
                for unit, raw in zip(group, chunks, strict=True):
                    result[unit.id] = self._detailed_vector(unit, raw) or self._l1_vector(unit)
        return result

    def _detailed_vector(self, unit: MemoryUnit, raw: bytes | None) -> list[float]:
        if raw is None or self._vector is None:
            return []
        ids = json.loads(raw)
        if not (isinstance(ids, list) and 0 < len(ids) <= 4096 and all(
                isinstance(uid, str) and uid.startswith(unit.id + "-") for uid in ids)):
            return []
        records = self._vector.get(unit.scope, ids)
        if (len(records) == len(set(ids)) and {r.id for r in records} == set(ids)
                and all(self._valid(r, unit, unit.content) for r in records)):
            return mean_vector([r.vector for r in records])
        return []

    def _l1_vector(self, unit: MemoryUnit) -> list[float]:
        if self._layers_l1 is not None and unit.layers.l1:
            uid = layer_record_id(unit.id, "l1")
            records = self._layers_l1.get(unit.scope, [uid])
            if (len(records) == 1 and records[0].id == uid
                    and self._valid(records[0], unit, unit.layers.l1)):
                return mean_vector([records[0].vector])
        return []
