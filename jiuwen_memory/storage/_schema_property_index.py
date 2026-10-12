# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Schema Entity to Property MemoryUnit derived reverse index."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.schema_property import (
    SchemaPropertyIdentity,
    schema_property_identity,
)
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory_codec import loads

from .kv import KVStore

_PREFIX = "/schema/entity-properties/v1"
_SCOPE_WATERMARK_KEY = f"{_PREFIX}/scope-watermark"


@dataclass
class _ScopeLockEntry:
    lock: threading.RLock
    users: int = 0


_LOCKS: dict[tuple[int, str, str, str, str, str], _ScopeLockEntry] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class SchemaPropertyLookup:
    """Result of one Entity-to-Property reverse-index lookup."""

    indexed: bool
    unit_ids: list[str]


class SchemaPropertyIndex:
    """Maintain Entity-to-Property ids in the scope-partitioned KV truth store.

    One scope watermark is the commit marker for the complete reverse-index view.
    Before the first incremental write in a legacy scope, all Property units are
    backfilled by one scope scan. A failed mutation leaves the watermark absent,
    so readers use their compatible truth-store fallback instead of trusting a
    partially updated derived index.
    """

    def __init__(self, kv: KVStore) -> None:
        self._kv = kv

    def upsert(self, units: list[MemoryUnit]) -> None:
        """Synchronize current Property ownership after a successful index write."""

        for scope, scoped_units in _group_by_scope(units):
            with self._scope_lock(scope):
                identities = [schema_property_identity(unit) for unit in scoped_units]
                if not self._scope_indexed(scope):
                    if any(identity is not None for identity in identities):
                        self._bootstrap_scope(scope, scoped_units)
                    else:
                        self._remove_units_without_commit(scope, scoped_units)
                    continue
                self._begin_mutation(scope)
                for unit, current in zip(scoped_units, identities, strict=True):
                    self._sync_unit(scope, unit.id, current)
                self._commit_scope(scope)

    def mark_forward_only(self, units: list[MemoryUnit]) -> None:
        """Exclude Property truth intentionally written without retrieval indexes."""

        for scope, scoped_units in _group_by_scope(units):
            with self._scope_lock(scope):
                indexed = self._scope_indexed(scope)
                if indexed:
                    self._begin_mutation(scope)
                changed = False
                for unit in scoped_units:
                    current = schema_property_identity(unit)
                    previous = self._read_pointer(scope, unit.id)
                    if current is None and previous is None:
                        continue
                    changed = True
                    self._delete_unit(scope, unit.id, previous or current)
                    self._put(
                        scope,
                        _exclusion_key(unit.id),
                        unit.id.encode("utf-8"),
                    )
                if indexed:
                    self._commit_scope(scope)
                elif changed:
                    self._bootstrap_scope(scope, [])

    def remove(self, units: list[MemoryUnit]) -> None:
        """Remove unit memberships while retaining an authoritative scope view."""

        for scope, scoped_units in _group_by_scope(units):
            with self._scope_lock(scope):
                indexed = self._scope_indexed(scope)
                if indexed:
                    self._begin_mutation(scope)
                for unit in scoped_units:
                    identity = self._read_pointer(scope, unit.id)
                    if identity is None:
                        identity = schema_property_identity(unit)
                    self._delete_unit(scope, unit.id, identity)
                    self._kv.delete(scope, _exclusion_key(unit.id))
                if indexed:
                    self._commit_scope(scope)

    def remove_with_scope(self, scope: Scope, unit_ids: list[str]) -> None:
        """Remove memberships when a rollback path only has scope and unit ids."""

        if not unit_ids:
            return
        with self._scope_lock(scope):
            indexed = self._scope_indexed(scope)
            if indexed:
                self._begin_mutation(scope)
            for unit_id in dict.fromkeys(value for value in unit_ids if value):
                identity = self._read_pointer(scope, unit_id)
                self._delete_unit(scope, unit_id, identity)
                self._kv.delete(scope, _exclusion_key(unit_id))
            if indexed:
                self._commit_scope(scope)

    def lookup(self, scope: Scope, entity_key: str) -> SchemaPropertyLookup:
        """Return Property ids without scanning MemoryUnits when the scope is indexed."""

        normalized_key = entity_key.strip()
        if not normalized_key or not self._scope_indexed(scope):
            return SchemaPropertyLookup(indexed=False, unit_ids=[])
        prefix = _membership_prefix(normalized_key)
        unit_ids: set[str] = set()
        for _, raw_unit_id in self._kv.scan(scope, prefix):
            try:
                unit_id = raw_unit_id.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackendError("schema property index membership is not UTF-8") from exc
            if unit_id:
                unit_ids.add(unit_id)
        return SchemaPropertyLookup(indexed=True, unit_ids=sorted(unit_ids))

    def invalidate(self, units: list[MemoryUnit]) -> None:
        """Make affected scopes fall back after a partially failed owner write."""

        scopes: dict[tuple[str, str, str, str, str], Scope] = {}
        for unit in units:
            scopes.setdefault(_scope_key(unit.scope), unit.scope)
        for key in sorted(scopes):
            scope = scopes.get(key)
            if scope is not None:
                self.invalidate_scope(scope)

    def invalidate_scope(self, scope: Scope) -> None:
        """Remove only the derived commit marker; memberships remain rebuildable."""

        with self._scope_lock(scope):
            self._begin_mutation(scope)

    def _bootstrap_scope(self, scope: Scope, current_units: list[MemoryUnit]) -> None:
        """Rebuild every Entity membership with one truth scan, then commit."""

        exclusions = self._read_exclusions(scope)
        current_ids = {unit.id for unit in current_units}
        exclusions.difference_update(current_ids)

        discovered: dict[str, MemoryUnit] = {}
        for _, raw in self._kv.scan(scope, MEMORY_KEY_PREFIX):
            try:
                unit = loads(raw)
            except Exception as exc:
                raise BackendError(
                    "schema property index bootstrap cannot decode MemoryUnit"
                ) from exc
            if unit is not None:
                discovered[unit.id] = unit
        # RETRIEVAL_ONLY writes may not exist in the forward store. Current
        # units also override an older truth value during update recovery.
        for unit in current_units:
            discovered[unit.id] = unit

        for unit_id in current_ids:
            self._kv.delete(scope, _exclusion_key(unit_id))
        exclusion_prefix = f"{_PREFIX}/excluded/"
        for key, _ in self._kv.scan(scope, f"{_PREFIX}/"):
            if not key.startswith(exclusion_prefix):
                self._kv.delete(scope, key)

        for unit in discovered.values():
            identity = schema_property_identity(unit)
            if identity is not None and unit.id not in exclusions:
                self._write_unit(scope, unit.id, identity)
        self._commit_scope(scope)

    def _sync_unit(
        self,
        scope: Scope,
        unit_id: str,
        current: SchemaPropertyIdentity | None,
    ) -> None:
        previous = self._read_pointer(scope, unit_id)
        self._kv.delete(scope, _exclusion_key(unit_id))
        if current is None:
            self._delete_unit(scope, unit_id, previous)
            return

        # Publish the new owner first. If deleting the previous owner fails,
        # a reader may see one harmless duplicate; it rechecks canonical owner
        # metadata after materialization. The inverse order can lose the unit.
        self._write_unit(scope, unit_id, current)
        if previous is not None and previous.entity_key != current.entity_key:
            self._delete_membership(scope, previous, unit_id)

    def _write_unit(
        self,
        scope: Scope,
        unit_id: str,
        identity: SchemaPropertyIdentity,
    ) -> None:
        self._put(
            scope,
            _membership_key(identity.entity_key, unit_id),
            unit_id.encode("utf-8"),
        )
        self._put(scope, _pointer_key(unit_id), _encode_pointer(identity, unit_id))

    def _remove_units_without_commit(
        self,
        scope: Scope,
        units: list[MemoryUnit],
    ) -> None:
        """Best-effort cleanup while preserving legacy-fallback semantics."""

        for unit in units:
            previous = self._read_pointer(scope, unit.id)
            self._delete_unit(scope, unit.id, previous)
            self._kv.delete(scope, _exclusion_key(unit.id))

    def _delete_unit(
        self,
        scope: Scope,
        unit_id: str,
        identity: SchemaPropertyIdentity | None,
    ) -> None:
        if identity is not None:
            self._delete_membership(scope, identity, unit_id)
        self._kv.delete(scope, _pointer_key(unit_id))

    def _read_pointer(self, scope: Scope, unit_id: str) -> SchemaPropertyIdentity | None:
        try:
            raw = self._kv.get(scope, _pointer_key(unit_id))
        except NotFoundError:
            return None
        value = _decode(raw, kind="pointer")
        if value.get("unit_id") != unit_id or not isinstance(value.get("entity_key"), str):
            raise BackendError("schema property index pointer does not match unit id")
        return SchemaPropertyIdentity(
            entity_key=value["entity_key"],
            entity_name=_string(value.get("entity_name")),
            entity_type=_string(value.get("entity_type")),
        )

    def _scope_indexed(self, scope: Scope) -> bool:
        try:
            raw = self._kv.get(scope, _SCOPE_WATERMARK_KEY)
        except NotFoundError:
            return False
        value = _decode(raw, kind="scope watermark")
        if value.get("version") != "1" or value.get("state") != "complete":
            raise BackendError("schema property index scope watermark is invalid")
        return True

    def _read_exclusions(self, scope: Scope) -> set[str]:
        unit_ids: set[str] = set()
        for _, raw in self._kv.scan(scope, f"{_PREFIX}/excluded/"):
            try:
                unit_id = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackendError("schema property exclusion is not UTF-8") from exc
            if unit_id:
                unit_ids.add(unit_id)
        return unit_ids

    def _begin_mutation(self, scope: Scope) -> None:
        self._kv.delete(scope, _SCOPE_WATERMARK_KEY)

    def _commit_scope(self, scope: Scope) -> None:
        self._put(
            scope,
            _SCOPE_WATERMARK_KEY,
            _encode({"state": "complete", "version": "1"}),
        )

    def _delete_membership(
        self,
        scope: Scope,
        identity: SchemaPropertyIdentity,
        unit_id: str,
    ) -> None:
        self._kv.delete(scope, _membership_key(identity.entity_key, unit_id))

    def _put(self, scope: Scope, key: str, value: bytes) -> None:
        """Implement idempotent upsert through the common KV CRUD contract."""

        try:
            self._kv.update(scope, key, value)
        except NotFoundError:
            try:
                self._kv.insert(scope, key, value)
            except ConflictError:
                self._kv.update(scope, key, value)

    @contextmanager
    def _scope_lock(self, scope: Scope):
        key = (id(self._kv), *_scope_key(scope))
        with _LOCKS_GUARD:
            entry = _LOCKS.setdefault(key, _ScopeLockEntry(threading.RLock()))
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with _LOCKS_GUARD:
                entry.users -= 1
                if entry.users == 0 and _LOCKS.get(key) is entry:
                    del _LOCKS[key]


def _group_by_scope(units: list[MemoryUnit]) -> list[tuple[Scope, list[MemoryUnit]]]:
    grouped: dict[tuple[str, str, str, str, str], tuple[Scope, list[MemoryUnit]]] = {}
    for unit in units:
        key = _scope_key(unit.scope)
        if key not in grouped:
            grouped[key] = (unit.scope, [])
        grouped[key][1].append(unit)
    return list(grouped.values())


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _membership_prefix(entity_key: str) -> str:
    return f"{_PREFIX}/entities/{_digest(entity_key)}/members/"


def _membership_key(entity_key: str, unit_id: str) -> str:
    return f"{_membership_prefix(entity_key)}{_digest(unit_id)}"


def _pointer_key(unit_id: str) -> str:
    return f"{_PREFIX}/units/{_digest(unit_id)}"


def _exclusion_key(unit_id: str) -> str:
    return f"{_PREFIX}/excluded/{_digest(unit_id)}"


def _encode_pointer(identity: SchemaPropertyIdentity, unit_id: str) -> bytes:
    return _encode(
        {
            "entity_key": identity.entity_key,
            "entity_name": identity.entity_name,
            "entity_type": identity.entity_type,
            "unit_id": unit_id,
        }
    )


def _encode(value: dict[str, str]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _decode(raw: bytes, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackendError(f"schema property index {kind} is corrupted") from exc
    if not isinstance(value, dict):
        raise BackendError(f"schema property index {kind} is not an object")
    return value


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
