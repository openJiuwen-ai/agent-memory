# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Persistent membership snapshots for one document-memory root."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from jiuwen_memory.common.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.kv import KVStore

DimensionKind = Literal["project", "team", "group"]
MemberStatus = Literal["active", "departed"]


@dataclass(frozen=True, order=True)
class Dimension:
    org: str
    space: str
    kind: DimensionKind
    id: str

    def __post_init__(self) -> None:
        valid_home = isinstance(self.org, str) and bool(self.org) and isinstance(self.space, str)
        valid_key = isinstance(self.id, str) and bool(self.id.strip())
        if not valid_home or not valid_key or self.kind not in ("project", "team", "group"):
            raise ValidationError("Invalid collective dimension")


@dataclass(frozen=True)
class Member:
    scope: Scope
    role: Literal["manager", "member"] = "member"
    status: MemberStatus = "active"

    def __post_init__(self) -> None:
        if self.role not in ("manager", "member") or self.status not in ("active", "departed"):
            raise ValidationError("Invalid collective member role or status")


class MemberStore(Protocol):
    def initialize(
        self, dimension: Dimension, members: Sequence[Member], *, host_managed: bool = False,
    ) -> tuple[Member, ...]:
        ...

    def upsert(
        self, dimension: Dimension, members: Sequence[Member], *, operator=None,
    ) -> tuple[Member, ...]:
        ...

    def depart(self, dimension: Dimension, subjects: Sequence[Scope], *, operator=None) -> int:
        ...

    def members(self, dimension: Dimension) -> tuple[Member, ...]:
        ...

    def active_dimensions(self, scope: Scope) -> tuple[Dimension, ...]:
        ...

    def contributors(self, caller: Scope) -> tuple[tuple[Dimension, Scope], ...]:
        ...

    def close(self) -> None:
        ...


class KVBackedMemberStore:
    """Commit rosters and their reverse view as one CAS record per real home."""

    _KEY = "/collective/members"

    def __init__(self, kv: KVStore) -> None:
        if not kv.supports_compare_and_swap():
            raise ValidationError("Collective membership requires atomic KV replacement")
        self._kv = kv

    def close(self) -> None:
        self._kv.close()

    def _snapshot(self, home: Scope) -> tuple[bytes | None, dict]:
        try:
            raw = self._kv.get(home, self._KEY)
        except NotFoundError:
            return None, {}
        return raw, json.loads(raw)

    def _mutate(self, dimension: Dimension, update: Callable) -> object:
        home = Scope(org=dimension.org, space=dimension.space)
        for _ in range(16):
            raw, snapshot = self._snapshot(home)
            rosters = snapshot.setdefault("rosters", {})
            dimensions = rosters.setdefault(dimension.kind, {})
            current = self._decode(dimension, dimensions.get(dimension.id, []))
            updated, result = update(current)
            if updated == current:
                return result
            dimensions[dimension.id] = [
                [m.scope.user, m.scope.agent, m.role, m.status] for m in updated
            ]
            reverse = snapshot.setdefault("active", {})
            scope_keys = {(m.scope.user, m.scope.agent) for m in (*current, *updated)}
            active_keys = {(m.scope.user, m.scope.agent) for m in updated if m.status == "active"}
            label = [dimension.kind, dimension.id]
            for principal in scope_keys:
                subject = json.dumps(principal, separators=(",", ":"))
                matches = [item for item in reverse.get(subject, []) if item != label]
                if principal in active_keys:
                    matches.append(label)
                if matches:
                    reverse[subject] = matches
                else:
                    reverse.pop(subject, None)
            encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
            if self._kv.compare_and_swap(home, self._KEY, raw, encoded):
                return result
        raise ConflictError("collective roster changed concurrently")

    @staticmethod
    def _decode(dimension: Dimension, rows: list) -> tuple[Member, ...]:
        return tuple(
            Member(Scope(org=dimension.org, space=dimension.space, user=u, agent=a), role, status)
            for u, a, role, status in rows
        )

    @staticmethod
    def _validate(dimension: Dimension, scopes: Sequence[Scope]) -> None:
        for scope in scopes:
            if (scope.org, scope.space) != (dimension.org, dimension.space) or scope.session:
                raise ValidationError("Collective subject is outside its dimension")
            if not (scope.user or scope.agent):
                raise ValidationError("Collective subject has no identity")

    def initialize(
        self, dimension: Dimension, members: Sequence[Member], *, host_managed: bool = False,
    ) -> tuple[Member, ...]:
        if not host_managed and not any(m.role == "manager" and m.status == "active" for m in members):
            raise ValidationError("Collective dimension needs an active manager")
        return self._enroll(dimension, members, operator=None, reactivate=False)

    def upsert(
        self, dimension: Dimension, members: Sequence[Member], *, operator=None,
    ) -> tuple[Member, ...]:
        return self._enroll(dimension, members, operator=operator, reactivate=True)

    def _enroll(self, dimension, members, *, operator, reactivate):
        self._validate(dimension, [member.scope for member in members])
        if any(member.status != "active" for member in members):
            raise ValidationError("Enrollment requires active member records")

        def update(current):
            if operator is not None and not any(
                m.scope == operator and m.role == "manager" and m.status == "active" for m in current
            ):
                raise PermissionDeniedError("collective enrollment requires a manager")
            records = {(m.scope.user, m.scope.agent): m for m in current}
            for member in members:
                key = (member.scope.user, member.scope.agent)
                previous = records.get(key)
                if previous and previous.status == "departed" and not reactivate:
                    continue
                if previous and previous.role == "manager":
                    member = replace(member, role="manager")
                records[key] = member
            updated = tuple(records.values())
            return updated, updated

        return self._mutate(dimension, update)

    def depart(self, dimension: Dimension, subjects: Sequence[Scope], *, operator=None) -> int:
        self._validate(dimension, subjects)

        def update(current):
            if operator is not None and any(scope != operator for scope in subjects) and not any(
                m.scope == operator and m.role == "manager" and m.status == "active" for m in current
            ):
                raise PermissionDeniedError("collective departure requires a manager")
            updated = tuple(
                replace(m, status="departed") if m.scope in subjects else m for m in current
            )
            return updated, sum(a.status != b.status for a, b in zip(current, updated, strict=True))

        return self._mutate(dimension, update)

    def members(self, dimension: Dimension) -> tuple[Member, ...]:
        _, snapshot = self._snapshot(Scope(org=dimension.org, space=dimension.space))
        rows = snapshot.get("rosters", {}).get(dimension.kind, {}).get(dimension.id, [])
        return self._decode(dimension, rows)

    def _memberships(self, caller: Scope):
        _, snapshot = self._snapshot(Scope(org=caller.org, space=caller.space))
        subject = json.dumps((caller.user, caller.agent), separators=(",", ":"))
        rosters = snapshot.get("rosters", {})
        for kind, key in snapshot.get("active", {}).get(subject, []):
            dimension = Dimension(caller.org, caller.space, kind, key)
            rows = rosters[kind][key]
            yield dimension, self._decode(dimension, rows)

    def active_dimensions(self, scope: Scope) -> tuple[Dimension, ...]:
        return tuple(dimension for dimension, _ in self._memberships(scope))

    def contributors(self, caller: Scope) -> tuple[tuple[Dimension, Scope], ...]:
        return tuple((dimension, member.scope) for dimension, roster in self._memberships(caller)
                     for member in roster)
