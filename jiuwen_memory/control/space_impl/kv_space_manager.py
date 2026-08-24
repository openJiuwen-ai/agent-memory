# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""KV-backed SpaceManager implementation."""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX, Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.control.types import (
    PrincipalPath,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)
from jiuwen_memory.storage.storage import Storage, StorageProducer

_INFO_KEY = "/space/info"
_MEMBER_PREFIX = "/space/members/"
_EXPORT_PREFIX = "/space/exports/"
_REGISTRY_PREFIX = "/spaces/by-id/"
_ROOT_SCOPE = Scope()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(org: str, space: str) -> Scope:
    return Scope(org=org, space=space)


def _validate_space(org: str, space: str) -> None:
    if not org:
        raise ValidationError("org is required")
    if not space:
        raise ValidationError("space is required")


def _dt(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _scope_to_dict(scope: Scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": scope.space,
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _scope_from_dict(data: dict[str, str]) -> Scope:
    return Scope(
        org=str(data.get("org", "")),
        space=str(data.get("space", "")),
        user=str(data.get("user", "")),
        agent=str(data.get("agent", "")),
        session=str(data.get("session", "")),
    )


def _policy_to_dict(policy: SpacePolicy) -> dict[str, object]:
    return {
        "require_space": policy.require_space,
        "principal_path": policy.principal_path.value,
        "storage_isolation_strategy": policy.storage_isolation_strategy,
        "retention": dict(policy.retention),
        "quotas": dict(policy.quotas),
        "index_profiles": dict(policy.index_profiles),
        "pipeline_profiles": dict(policy.pipeline_profiles),
    }


def _policy_from_dict(data: dict[str, object] | None) -> SpacePolicy:
    data = data or {}
    return SpacePolicy(
        require_space=bool(data.get("require_space", False)),
        principal_path=PrincipalPath(
            str(data.get("principal_path", PrincipalPath.USER_AGENT.value))
        ),
        storage_isolation_strategy=str(data.get("storage_isolation_strategy", "metadata_filter")),
        retention={str(k): str(v) for k, v in dict(data.get("retention", {}) or {}).items()},
        quotas={str(k): str(v) for k, v in dict(data.get("quotas", {}) or {}).items()},
        index_profiles={
            str(k): str(v) for k, v in dict(data.get("index_profiles", {}) or {}).items()
        },
        pipeline_profiles={
            str(k): str(v) for k, v in dict(data.get("pipeline_profiles", {}) or {}).items()
        },
    )


def _info_to_bytes(info: SpaceInfo) -> bytes:
    payload = {
        "org": info.org,
        "space": info.space,
        "display_name": info.display_name,
        "status": info.status.value,
        "principal_path": info.principal_path.value,
        "policy": _policy_to_dict(info.policy),
        "metadata": dict(info.metadata),
        "created_at": _dt(info.created_at),
        "archived_at": _dt(info.archived_at),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _info_from_bytes(raw: bytes) -> SpaceInfo:
    data = json.loads(raw.decode("utf-8"))
    policy = _policy_from_dict(data.get("policy"))
    principal_path = PrincipalPath(str(data.get("principal_path", policy.principal_path.value)))
    policy = replace(policy, principal_path=principal_path)
    return SpaceInfo(
        org=str(data.get("org", "")),
        space=str(data.get("space", "")),
        display_name=str(data.get("display_name", "")),
        status=SpaceStatus(str(data.get("status", SpaceStatus.ACTIVE.value))),
        principal_path=principal_path,
        policy=policy,
        metadata={str(k): str(v) for k, v in dict(data.get("metadata", {}) or {}).items()},
        created_at=_parse_dt(data.get("created_at")),
        archived_at=_parse_dt(data.get("archived_at")),
    )


def _member_to_bytes(member: SpaceMember) -> bytes:
    payload = {
        "scope": _scope_to_dict(member.scope),
        "role": member.role,
        "created_at": _dt(member.created_at),
        "expires_at": _dt(member.expires_at),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _member_from_bytes(raw: bytes) -> SpaceMember:
    data = json.loads(raw.decode("utf-8"))
    return SpaceMember(
        scope=_scope_from_dict(dict(data.get("scope", {}) or {})),
        role=str(data.get("role", "member")),
        created_at=_parse_dt(data.get("created_at")),
        expires_at=_parse_dt(data.get("expires_at")),
    )


def _member_key(scope: Scope) -> str:
    payload = json.dumps(
        _scope_to_dict(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_MEMBER_PREFIX}{token}"


def _registry_key(space: str) -> str:
    token = base64.urlsafe_b64encode(space.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_REGISTRY_PREFIX}{token}"


def _normalize_member(org: str, space: str, member: SpaceMember) -> SpaceMember:
    scope = replace(member.scope)
    if scope.org and scope.org != org:
        raise ValidationError("member scope org must match target space org")
    if scope.space and scope.space != space:
        raise ValidationError("member scope space must match target space")
    scope.org = org
    scope.space = space
    created_at = member.created_at or _now()
    return SpaceMember(
        scope=scope,
        role=member.role or "member",
        created_at=created_at,
        expires_at=member.expires_at,
    )


def _normalize_member_scope(org: str, space: str, member: Scope) -> Scope:
    scope = replace(member)
    if scope.org and scope.org != org:
        raise ValidationError("member scope org must match target space org")
    if scope.space and scope.space != space:
        raise ValidationError("member scope space must match target space")
    scope.org = org
    scope.space = space
    return scope


class KVSpaceManager(SpaceManager):
    """SpaceManager backed by KVStore."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._kv = storage.kv

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SPACE

    def health(self) -> None:
        self._kv.health()

    def create(self, spec: SpaceSpec) -> SpaceInfo:
        _validate_space(spec.org, spec.space)
        scope = _scope(spec.org, spec.space)
        registry_key = _registry_key(spec.space)
        if self._kv.exists(_ROOT_SCOPE, registry_key):
            raise ConflictError("space", spec.space)
        for candidate in self._kv.scopes():
            if candidate.space != spec.space:
                continue
            info_scope = _scope(candidate.org, candidate.space)
            if self._kv.exists(info_scope, _INFO_KEY):
                raise ConflictError("space", spec.space)
        if self._kv.exists(scope, _INFO_KEY):
            raise ConflictError("space", spec.space)
        policy = replace(spec.policy, principal_path=spec.principal_path)
        info = SpaceInfo(
            org=spec.org,
            space=spec.space,
            display_name=spec.display_name or spec.space,
            status=SpaceStatus.ACTIVE,
            principal_path=spec.principal_path,
            policy=policy,
            metadata={str(k): str(v) for k, v in spec.metadata.items()},
            created_at=_now(),
        )
        self._kv.insert(_ROOT_SCOPE, registry_key, spec.org.encode("utf-8"))
        try:
            self._kv.insert(scope, _INFO_KEY, _info_to_bytes(info))
        except Exception:
            self._kv.delete(_ROOT_SCOPE, registry_key)
            raise
        return info

    def get(self, org: str, space: str) -> SpaceInfo:
        _validate_space(org, space)
        try:
            return _info_from_bytes(self._kv.get(_scope(org, space), _INFO_KEY))
        except NotFoundError:
            raise NotFoundError("space", f"{org}/{space}") from None

    def list(
        self,
        org: str,
        *,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        if not org:
            raise ValidationError("org is required")
        if limit <= 0:
            raise ValidationError("limit must be > 0")
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise ValidationError(f"cursor must be an integer offset, got {cursor!r}") from None
        if offset < 0:
            raise ValidationError("cursor must be >= 0")

        spaces: dict[tuple[str, str], SpaceInfo] = {}
        for scope in self._kv.scopes():
            if scope.org != org or not scope.space:
                continue
            try:
                info = self.get(scope.org, scope.space)
            except NotFoundError:
                continue
            if status is not None and info.status != status:
                continue
            spaces[(info.org, info.space)] = info
        ordered = [spaces[key] for key in sorted(spaces)]
        return ordered[offset:offset + limit]

    def update(self, org: str, space: str, patch: SpacePatch) -> SpaceInfo:
        info = self.get(org, space)
        if patch.display_name is not None:
            info.display_name = patch.display_name
        if patch.policy is not None:
            info.policy = patch.policy
            info.principal_path = patch.policy.principal_path
        if patch.principal_path is not None:
            info.principal_path = patch.principal_path
            info.policy = replace(info.policy, principal_path=patch.principal_path)
        if patch.metadata is not None:
            info.metadata.update({str(k): str(v) for k, v in patch.metadata.items()})
        if patch.status is not None:
            info.status = patch.status
            if patch.status == SpaceStatus.ARCHIVED and info.archived_at is None:
                info.archived_at = _now()
        self._kv.update(_scope(org, space), _INFO_KEY, _info_to_bytes(info))
        return info

    def archive(self, org: str, space: str) -> SpaceInfo:
        return self.update(org, space, SpacePatch(status=SpaceStatus.ARCHIVED))

    def delete(self, org: str, space: str) -> SpaceDeleteResult:
        self.get(org, space)
        counts = {
            "memory": 0,
            "message": 0,
            "space_metadata": 0,
            "kv": 0,
            "index": 0,
        }
        target_scopes = [
            scope for scope in self._kv.scopes() if scope.org == org and scope.space == space
        ]
        if not target_scopes:
            target_scopes = [_scope(org, space)]
        for scope in target_scopes:
            for key, _ in list(self._kv.scan(scope)):
                if key.startswith(MEMORY_KEY_PREFIX):
                    counts["memory"] += 1
                elif key.startswith(MESSAGES_KEY_PREFIX):
                    counts["message"] += 1
                elif key.startswith("/space/"):
                    counts["space_metadata"] += 1
                counts["kv"] += 1
                self._kv.delete(scope, key)
        registry_key = _registry_key(space)
        if self._kv.exists(_ROOT_SCOPE, registry_key):
            self._kv.delete(_ROOT_SCOPE, registry_key)
            counts["space_metadata"] += 1
            counts["kv"] += 1
        return SpaceDeleteResult(org=org, space=space, deleted_counts=counts)

    def export(self, org: str, space: str, *, include_audit: bool = True) -> str:
        info = self.get(org, space)
        export_id = str(uuid.uuid4())
        payload = {
            "id": export_id,
            "org": org,
            "space": space,
            "include_audit": include_audit,
            "created_at": _dt(_now()),
            "status": "created",
            "space_status": info.status.value,
        }
        self._kv.insert(
            _scope(org, space),
            f"{_EXPORT_PREFIX}{export_id}",
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        return export_id

    def usage(self, org: str, space: str) -> SpaceUsage:
        _validate_space(org, space)
        usage = SpaceUsage(org=org, space=space)
        for scope in self._kv.scopes():
            if scope.org != org or scope.space != space:
                continue
            for key, value in self._kv.scan(scope):
                usage.storage_bytes += len(value)
                if key.startswith(MEMORY_KEY_PREFIX):
                    usage.memory_count += 1
                elif key.startswith(MESSAGES_KEY_PREFIX):
                    usage.message_count += 1
        return usage

    def get_policy(self, org: str, space: str) -> SpacePolicy:
        return self.get(org, space).policy

    def set_policy(self, org: str, space: str, policy: SpacePolicy) -> SpacePolicy:
        info = self.update(
            org,
            space,
            SpacePatch(policy=policy, principal_path=policy.principal_path),
        )
        return info.policy

    def list_members(self, org: str, space: str) -> list[SpaceMember]:
        self.get(org, space)
        members = [
            _member_from_bytes(raw)
            for _, raw in self._kv.scan(_scope(org, space), prefix=_MEMBER_PREFIX)
        ]
        members.sort(
            key=lambda member: (
                member.scope.user,
                member.scope.agent,
                member.scope.session,
                member.role,
            )
        )
        return members

    def add_member(self, org: str, space: str, member: SpaceMember) -> None:
        self.get(org, space)
        normalized = _normalize_member(org, space, member)
        key = _member_key(normalized.scope)
        value = _member_to_bytes(normalized)
        if self._kv.exists(_scope(org, space), key):
            self._kv.update(_scope(org, space), key, value)
            return
        self._kv.insert(_scope(org, space), key, value)

    def remove_member(self, org: str, space: str, member: Scope) -> None:
        self.get(org, space)
        normalized = _normalize_member_scope(org, space, member)
        self._kv.delete(_scope(org, space), _member_key(normalized))


@SpaceProducer.register("kv")
def _build(config):
    return KVSpaceManager(StorageProducer.resolve(config))
