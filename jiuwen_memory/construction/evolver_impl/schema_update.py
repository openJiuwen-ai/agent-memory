# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Schema source reconciliation, durable retries and old-evidence replay guards.

Only memory bodies go through IndexBuilder. Private operation records use the
injected KV port. This is a recoverable sequence, not a multi-backend transaction.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ConflictError, PartialFailureError, ValidationError
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory import MEMORY_KEY_PREFIX
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.construction.source_update import (
    STRICT_ENTITY_WRITES,
    SourceExtraction,
    SourceUpdatePlan,
    UnitChange,
)
from jiuwen_memory.storage.kv import KVStore, load_units
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

_PREFIX = "/schema_updates/"


def content_revision(unit: MemoryUnit) -> str:
    # Explicit overwrites can return to the same text (A -> B -> A). The latest A
    # is distinct from a replay of the original A, even though its ID is unchanged.
    revision = unit.system_metadata.get("schema_source_revision", "")
    return hashlib.sha256(f"{revision}\0{unit.content}".encode("utf-8")).hexdigest()


def _revision(unit: MemoryUnit) -> str:
    # Index builders can fill vectors/index_metadata in place; those are not input revisions.
    data = [
        unit.id,
        unit.content,
        unit.entities,
        unit.provenance,
        unit.source_ref,
        unit.supersedes,
        unit.lifecycle.value,
        str(unit.temporal),
        unit.tier.value,
        unit.tags,
        unit.user_metadata,
        {k: v for k, v in unit.system_metadata.items() if not k.startswith("index_")},
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def property_key(unit: MemoryUnit) -> tuple[str, ...]:
    meta = unit.system_metadata
    return tuple(
        str(meta.get(k) or "").strip().casefold()
        for k in ("schema_name", "schema_entity_type", "schema_entity_name", "schema_property_name")
    )


def _fact_key(unit: MemoryUnit) -> tuple[str, str]:
    return " ".join(unit.content.split()).casefold(), str(unit.temporal.t_event or "")


def sources(unit: MemoryUnit) -> list[str]:
    return list(dict.fromkeys(unit.provenance or ([unit.source_ref] if unit.source_ref else [])))


def _scope_key(scope: Scope) -> tuple[str, ...]:
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _encode_unit(unit: MemoryUnit | None) -> str | None:
    return dumps(unit).decode("utf-8") if unit is not None else None


def _decode_unit(value: str | None) -> MemoryUnit | None:
    return loads(value.encode("utf-8")) if value is not None else None


def _payload(plan: SourceUpdatePlan, *, done: bool = False) -> bytes:
    if done:
        # Completed guards retain hashes and IDs, not an extra copy of overwritten memory bodies.
        return json.dumps(
            {
                "operation_id": plan.operation_id,
                "request_key": plan.request_key,
                "source_id": plan.source_before.id,
                "source_revision": content_revision(plan.source_before),
                "source_after_id": plan.source_after.id,
                "source_after_revision": content_revision(plan.source_after),
                "guards": plan.guards,
                "completed": plan.completed,
                "done": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    return json.dumps(
        {
            "operation_id": plan.operation_id,
            "request_key": plan.request_key,
            "source_before": _encode_unit(plan.source_before),
            "source_after": _encode_unit(plan.source_after),
            "changes": [[_encode_unit(c.before), _encode_unit(c.after)] for c in plan.changes],
            "guards": plan.guards,
            "completed": plan.completed,
            "done": done,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _plan(data: dict) -> SourceUpdatePlan:
    return SourceUpdatePlan(
        data["operation_id"],
        data["request_key"],
        _decode_unit(data["source_before"]),
        _decode_unit(data["source_after"]),
        [UnitChange(_decode_unit(a), _decode_unit(b)) for a, b in data["changes"]],
        data["guards"],
        data["completed"],
    )


class SchemaUpdateCoordinator:
    def __init__(self, kv: KVStore, extract: Callable[[MemoryUnit], SourceExtraction]) -> None:
        self._kv = kv
        self._extract = extract

    def _records(self, scope: Scope) -> list[dict]:
        return [json.loads(raw) for _, raw in self._kv.scan(scope, _PREFIX)]

    def prepare(
        self, old: MemoryUnit, new: MemoryUnit, *, mode: str, request_key: str
    ) -> SourceUpdatePlan | None:
        for record in self._records(old.scope):
            if record["done"]:
                if record["source_id"] == old.id and record["request_key"] == request_key:
                    result = load_units(self._kv, old.scope, [record["source_after_id"]])
                    if result and content_revision(result[0]) == record["source_after_revision"]:
                        if old.lifecycle == LifecycleState.SUPERSEDED and old.id != result[0].id:
                            return SourceUpdatePlan(
                                record["operation_id"], request_key, old, result[0], []
                            )
            else:
                pending = _plan(record)
                if pending.source_before.id == old.id and pending.request_key == request_key:
                    return pending
                raise ConflictError(
                    message="resume the pending Schema update before another update"
                )
        if old.content == new.content:
            return None
        if old.lifecycle != LifecycleState.ACTIVE:
            raise ValidationError("Schema content update requires an active source")
        supersede = mode == "supersede"
        if supersede:
            new.id = str(uuid.uuid4())
            new.supersedes = old.id
            new.lifecycle = LifecycleState.ACTIVE
        boundary = new.temporal.t_valid if supersede else datetime.now(timezone.utc)
        if boundary is None:
            raise ValidationError("Schema update requires a validity boundary")
        related = []
        for _, raw in self._kv.scan(old.scope, MEMORY_KEY_PREFIX):
            unit = loads(raw)
            if unit is None:
                continue
            if (
                unit.id == old.id
                or unit.system_metadata.get("extraction_mode") != "schema"
                or unit.lifecycle != LifecycleState.ACTIVE
            ):
                continue
            if old.id not in sources(unit):
                continue
            if unit.temporal.t_invalid is not None and unit.temporal.t_invalid <= boundary:
                continue
            expected = (
                new.system_metadata.get("schema_name"),
                new.system_metadata.get("schema_version"),
            )
            actual = (
                unit.system_metadata.get("schema_name"),
                unit.system_metadata.get("schema_version"),
            )
            if expected != actual:
                raise ValidationError(
                    "Associated properties use a different Schema configuration"
                )
            related.append(unit)
        new.system_metadata["schema_source_revision"] = str(uuid.uuid4())
        extraction = self._extract(copy.deepcopy(new))
        candidates = extraction.properties
        for unit in candidates:
            if unit.scope != new.scope or sources(unit) != [new.id]:
                raise ValidationError("Schema update candidates must reference only the new source")
            if not all(property_key(unit)):
                raise ValidationError("Schema update candidate has incomplete property identity")
        new.entities = list(dict.fromkeys(extraction.entities))
        new.layers.l0 = new.layers.l1 = ""
        new.vectors = []
        plan = SourceUpdatePlan(str(uuid.uuid4()), request_key, copy.deepcopy(old), new, [])
        old_groups: dict[tuple, list[MemoryUnit]] = defaultdict(list)
        new_groups: dict[tuple, list[MemoryUnit]] = defaultdict(list)
        for unit in related:
            old_groups[property_key(unit)].append(unit)
        for unit in candidates:
            new_groups[property_key(unit)].append(unit)
        for key in sorted(old_groups.keys() | new_groups.keys()):
            previous, incoming = list(old_groups[key]), list(new_groups[key])
            pairs = []
            # Exact facts first, then one-to-one event matches, then an unambiguous slot.
            for candidate in incoming[:]:
                matches = [u for u in previous if _fact_key(u) == _fact_key(candidate)]
                if len(matches) == 1:
                    pairs.append((matches[0], candidate))
                    previous.remove(matches[0])
                    incoming.remove(candidate)
            for candidate in incoming[:]:
                if candidate.temporal.t_event is None:
                    continue
                matches = [u for u in previous if u.temporal.t_event == candidate.temporal.t_event]
                peers = [u for u in incoming if u.temporal.t_event == candidate.temporal.t_event]
                if len(matches) == len(peers) == 1:
                    pairs.append((matches[0], candidate))
                    previous.remove(matches[0])
                    incoming.remove(candidate)
            if previous and incoming:
                if len(previous) != 1 or len(incoming) != 1:
                    raise ValidationError("ambiguous Schema property replacement")
                pairs.append((previous.pop(), incoming.pop()))
            for before, candidate in pairs:
                after = copy.deepcopy(candidate)
                same_fact = _fact_key(before) == _fact_key(candidate)
                if same_fact:
                    after.provenance = [new.id, *[sid for sid in sources(before) if sid != old.id]]
                after.user_metadata = {**before.user_metadata, **candidate.user_metadata}
                after.system_metadata = {**before.system_metadata, **candidate.system_metadata}
                self._replace(plan, before, after, supersede, boundary)
                self._guard(plan, before, only=old.id if same_fact else "")
            for before in previous:
                remaining = [sid for sid in sources(before) if sid != old.id]
                if remaining:
                    after = copy.deepcopy(before)
                    after.provenance = remaining
                    after.source_ref = remaining[0]
                    self._replace(plan, before, after, supersede, boundary)
                elif supersede:
                    retired = self._retire(before, boundary, replaced=False)
                    plan.changes.append(UnitChange(before, retired))
                else:
                    plan.changes.append(UnitChange(before, None))
                # Only the withdrawn revision, not other supporting sources, is blocked.
                self._guard(plan, before, only=old.id)
            for candidate in incoming:
                candidate.temporal.t_valid = boundary if supersede else candidate.temporal.t_valid
                plan.changes.append(UnitChange(None, candidate))
        # New properties first, then the source, then old versions/deletions.
        active = []
        rest = []
        for change in plan.changes:
            after = change.after
            if after is None or after.lifecycle != LifecycleState.ACTIVE:
                rest.append(change)
                continue
            if after.temporal.t_invalid is not None and after.temporal.t_invalid <= boundary:
                rest.append(change)
                continue
            active.append(change)
        plan.changes = active + [UnitChange(None if supersede else old, new)] + rest
        if supersede:
            plan.changes.append(UnitChange(old, self._retire(old, boundary, replaced=True)))
        for change in plan.changes:
            after = change.after
            if after is None:
                continue
            only_source_property = (
                after.id != new.id
                and after.lifecycle == LifecycleState.ACTIVE
                and sources(after) == [new.id]
            )
            source_expiry = new.temporal.t_invalid
            if only_source_property and source_expiry is not None:
                # A conclusion supported only by this source cannot outlive its validity.
                if after.temporal.t_invalid is None or source_expiry < after.temporal.t_invalid:
                    after.temporal.t_invalid = source_expiry
            valid_from = after.temporal.t_valid
            valid_until = after.temporal.t_invalid
            if valid_from is not None and valid_until is not None:
                if valid_until <= valid_from:
                    raise ValidationError("Schema update produces an inverted validity interval")
        return plan

    @staticmethod
    def _retire(before: MemoryUnit, boundary: datetime, *, replaced: bool) -> MemoryUnit:
        if before.temporal.t_valid is not None and boundary <= before.temporal.t_valid:
            raise ValidationError("Schema update boundary must follow the previous valid time")
        after = copy.deepcopy(before)
        if replaced:
            after.lifecycle = LifecycleState.SUPERSEDED
        if after.temporal.t_invalid is None or boundary < after.temporal.t_invalid:
            after.temporal.t_invalid = boundary
        return after

    def _replace(
        self,
        plan: SourceUpdatePlan,
        before: MemoryUnit,
        after: MemoryUnit,
        supersede: bool,
        boundary: datetime,
    ) -> None:
        after.layers.l0 = after.layers.l1 = ""
        after.vectors = []
        if supersede:
            after.id = str(uuid.uuid4())
            after.supersedes = before.id
            after.temporal.t_valid = boundary
            after.temporal.t_invalid = before.temporal.t_invalid
            plan.changes.append(UnitChange(None, after))
            plan.changes.append(UnitChange(before, self._retire(before, boundary, replaced=True)))
        else:
            after.id = before.id
            after.supersedes = before.supersedes
            after.temporal.t_valid = before.temporal.t_valid
            after.temporal.t_invalid = before.temporal.t_invalid
            plan.changes.append(UnitChange(before, after))

    def _guard(self, plan: SourceUpdatePlan, before: MemoryUnit, *, only: str = "") -> None:
        for sid in sources(before):
            if only and sid != only:
                continue
            evidence = load_units(self._kv, before.scope, [sid])
            if evidence:
                plan.guards.append(
                    {
                        "key": list(property_key(before)),
                        "source_id": sid,
                        "revision": content_revision(evidence[0]),
                    }
                )

    def filter_replayed(
        self, candidates: list[MemoryUnit], inputs: list[MemoryUnit]
    ) -> list[MemoryUnit]:
        if not inputs:
            return candidates
        guards_by_scope = {}
        for unit in inputs:
            guards = []
            for record in self._records(unit.scope):
                guards.extend(record["guards"])
            guards_by_scope[_scope_key(unit.scope)] = guards
        revisions = {(_scope_key(u.scope), u.id): content_revision(u) for u in inputs}
        result = []
        for unit in candidates:
            scope_key = _scope_key(unit.scope)
            key = property_key(unit)
            blocked = set()
            for guard in guards_by_scope.get(scope_key, []):
                if tuple(guard["key"]) != key:
                    continue
                if revisions.get((scope_key, guard["source_id"])) == guard["revision"]:
                    blocked.add(guard["source_id"])
            # A joint conclusion cannot be certified by simply dropping a blocked source.
            if not blocked.intersection(sources(unit)):
                result.append(unit)
        return result

    def filter_inputs(self, inputs: list[MemoryUnit]) -> list[MemoryUnit]:
        """A corrected source revision must never be re-extracted by background work."""
        result = []
        for unit in inputs:
            obsolete = []
            for record in self._records(unit.scope):
                if record["done"]:
                    obsolete.append((record["source_id"], record["source_revision"]))
                else:
                    old = _decode_unit(record["source_before"])
                    obsolete.append((old.id, content_revision(old)))
            if (unit.id, content_revision(unit)) not in obsolete:
                result.append(unit)
        return result

    def commit(
        self, plan: SourceUpdatePlan, *, index_for: Callable[[MemoryUnit], IndexBuilder]
    ) -> MemoryUnit:
        scope = plan.source_before.scope
        key = _PREFIX + plan.operation_id
        if self._kv.exists(scope, key):
            saved = json.loads(self._kv.get(scope, key))
            if saved["done"]:
                current = load_units(self._kv, scope, [saved["source_after_id"]])
                if not current:
                    raise ConflictError(
                        message="Completed Schema update result is no longer present"
                    )
                return current[0]
            plan = _plan(saved)
        else:
            for change in plan.changes:
                if change.before is not None:
                    current = load_units(self._kv, scope, [change.before.id])
                    if not current or _revision(current[0]) != _revision(change.before):
                        raise ConflictError(
                            message="Schema update inputs changed; retry preparation"
                        )
            current_source = load_units(self._kv, scope, [plan.source_before.id])
            if not current_source or _revision(current_source[0]) != _revision(plan.source_before):
                raise ConflictError(message="Schema source changed; retry preparation")
            self._kv.insert(scope, key, _payload(plan))
        token = STRICT_ENTITY_WRITES.set(True)
        try:
            for position in range(plan.completed, len(plan.changes)):
                change = plan.changes[position]
                if change.after is None:
                    index_for(change.before).remove([change.before])
                else:
                    unit = copy.deepcopy(change.after)
                    index = index_for(unit)
                    old_index = index_for(change.before) if change.before else index
                    exists = bool(load_units(self._kv, scope, [unit.id]))
                    if old_index is not index:
                        index.update([unit], mode=IndexWriteMode.FORWARD_ONLY)
                        old_index.remove([change.before], mode=IndexRemoveMode.SOFT)
                        index.build([unit], mode=IndexWriteMode.RETRIEVAL_ONLY)
                    elif exists:
                        index.update([unit])
                    else:
                        index.build([unit])
                plan.completed = position + 1
                self._kv.update(scope, key, _payload(plan))
            self._kv.update(scope, key, _payload(plan, done=True))
        except Exception as exc:
            raise PartialFailureError(
                completed=tuple(str(i) for i in range(plan.completed)),
                failed=f"schema_update:{plan.operation_id}",
                retry_action="update with the same patch",
            ) from exc
        finally:
            STRICT_ENTITY_WRITES.reset(token)
        return copy.deepcopy(plan.source_after)
