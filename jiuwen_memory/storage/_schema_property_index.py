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
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.memory_codec import loads

from .kv import KVStore

_PREFIX = "/schema/entity-properties/v1"
_SCOPE_WATERMARK_KEY = f"{_PREFIX}/scope-watermark"
# 修复标记：先于删除 watermark 写入。若 watermark 删除失败（后端故障），读侧仍能据此
# 判定索引不可信并回退真源，避免把缺成员的旧视图当成完整结果。
_SCOPE_REPAIR_KEY = f"{_PREFIX}/needs-repair"


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

    def __init__(self, kv: KVStore, *, lookup_batch_threshold: int = 8) -> None:
        self._kv = kv
        # 批量反查的实体数阈值：≤阈值时按每个实体走窄前缀 scan（只扫该实体 membership），
        # >阈值才走一次全 Scope scan。大 Scope + 少实体场景避免全量扫描退化。
        self._lookup_batch_threshold = max(1, int(lookup_batch_threshold))

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

    def lookup_batch(
        self,
        scope: Scope,
        entity_keys: list[str],
    ) -> dict[str, SchemaPropertyLookup]:
        """Return Property ids for many Entities.

        When the entity count is small (≤ ``lookup_batch_threshold``), each
        entity is resolved by its own narrow-prefix scan — only that entity's
        membership is touched. When the count exceeds the threshold, a single
        full-scope scan collects all of them at once, trading scan width for
        fewer round trips.
        """

        if not entity_keys:
            return {}
        normalized = list(dict.fromkeys(key.strip() for key in entity_keys))
        non_empty = [key for key in normalized if key]
        if not non_empty:
            return {key: SchemaPropertyLookup(indexed=False, unit_ids=[]) for key in normalized}
        if len(non_empty) <= self._lookup_batch_threshold:
            return {
                key: (
                    self.lookup(scope, key)
                    if key
                    else SchemaPropertyLookup(indexed=False, unit_ids=[])
                )
                for key in normalized
            }
        if not self._scope_indexed(scope):
            return {
                key: SchemaPropertyLookup(indexed=False, unit_ids=[]) for key in normalized
            }
        by_digest = {_digest(key): key for key in non_empty}
        grouped: dict[str, set[str]] = {key: set() for key in non_empty}
        for membership_key, raw_unit_id in self._kv.scan(scope, f"{_PREFIX}/entities/"):
            digest = _membership_entity_digest(membership_key)
            if digest is None:
                continue
            entity_key = by_digest.get(digest)
            if entity_key is None:
                continue
            try:
                unit_id = raw_unit_id.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackendError("schema property index membership is not UTF-8") from exc
            if unit_id:
                grouped[entity_key].add(unit_id)
        return {
            key: SchemaPropertyLookup(
                indexed=bool(key),
                unit_ids=sorted(grouped[key]),
            )
            for key in normalized
        }

    def invalidate(self, units: list[MemoryUnit]) -> None:
        """Make affected scopes fall back after a partially failed owner write.

        每个 scope 独立尝试：一个 scope 删除标记失败不能跳过其余 scope。全部尝试完后
        再抛第一个错误，调用方仍能看到清理失败。
        """

        scopes: dict[tuple[str, str, str, str, str], Scope] = {}
        for unit in units:
            scopes.setdefault(_scope_key(unit.scope), unit.scope)
        errors: list[Exception] = []
        for key in sorted(scopes):
            scope = scopes.get(key)
            if scope is None:
                continue
            try:
                self.invalidate_scope(scope)
            except Exception as exc:  # noqa: BLE001 —— 单个 scope 失败不阻断其余 scope
                errors.append(exc)
        if errors:
            first = errors[0]
            for extra in errors[1:]:
                first.add_note(
                    "additional scope invalidation failure: "
                    f"{type(extra).__name__}: {extra}"
                )
            raise first

    def invalidate_scope(self, scope: Scope) -> None:
        """Make the scope fall back to the truth store; memberships remain rebuildable.

        先写修复标记再删 watermark：删除失败时标记仍在，读侧不会信任旧索引。
        """

        with self._scope_lock(scope):
            self._put(scope, _SCOPE_REPAIR_KEY, b"1")
            self._begin_mutation(scope)

    def repair_scope(self, scope: Scope) -> None:
        """一次性修复旧版本坏数据：清全部 exclusion，失效后立即按真源重建。

        旧实现把批量写入的成功属性错误地 ``mark_forward_only``，留下
        watermark=complete、成员为空、exclusion 残留。清除**全部** exclusion：
        正常路径不会给 Property 写排除——归档/取代/退役走 ``update(FORWARD_ONLY)``，
        不调用 ``mark_forward_only``，且已归档成员必须留在索引里供历史回看；按生命周期
        筛选会让「被误排除、后又归档」的成员永久缺席。

        先失效（写 needs-repair）再重建：重建抛错时标记仍在，读侧继续回退真源，
        异常原样抛给调用方（维护工具据此报告失败）；成功则由 ``_commit_scope`` 清标记。
        """

        with self._scope_lock(scope):
            for key, _ in self._kv.scan(scope, f"{_PREFIX}/excluded/"):
                self._kv.delete(scope, key)
            self.invalidate_scope(scope)
            self._bootstrap_scope(scope, [])

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
            # needs-repair 必须活到新 watermark 落盘之后，扫除不能提前删它。
            if key == _SCOPE_REPAIR_KEY or key.startswith(exclusion_prefix):
                continue
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
        # 不缓存：SchemaPropertyIndex 跨检索/构建/演进多实例，缓存会让
        # invalidate 失效后读者仍信任派生索引（见 F07 已知遗留）。
        if self._kv.exists(scope, _SCOPE_REPAIR_KEY):
            return False
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
        # 先提交新 watermark，再清 needs-repair：反序会在中途留下「无标记 + 旧
        # watermark=complete」的窗口，读侧会把半成品视图当成完整结果。
        self._put(
            scope,
            _SCOPE_WATERMARK_KEY,
            _encode({"state": "complete", "version": "1"}),
        )
        self._kv.delete(scope, _SCOPE_REPAIR_KEY)

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


def _membership_entity_digest(membership_key: str) -> str | None:
    prefix = f"{_PREFIX}/entities/"
    if not membership_key.startswith(prefix):
        return None
    parts = membership_key[len(prefix):].split("/")
    if len(parts) != 3 or parts[1] != "members":
        return None
    entity_digest, _, unit_digest = parts
    if not entity_digest or not unit_digest:
        return None
    return entity_digest


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
