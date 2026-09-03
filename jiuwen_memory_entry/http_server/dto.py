# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Strict HTTP request DTOs and the external-to-domain Scope adapter.

The HTTP wire format deliberately keeps the integration vocabulary
(``tenant_id``/``scope``) while mapping it to the domain ``Scope`` vocabulary
inside the adapter.  This module never accepts or derives an actor: actor
identity is supplied separately by the authenticated request context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.types import RequestSecurityContext, Surface
from jiuwen_memory.common.type_def import Modality, Scope
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.types import DeleteMode, PrincipalPath, SpaceStatus
from jiuwen_memory_entry.core.dispatch_request import DispatchBatchItem, DispatchRequest

_RESERVED_IDENTITY_FIELDS = {
    "identity",
    "role",
    "acting_user",
    "principal",
    "authenticated_user",
}

_TARGET_FIELDS = {"tenant_id", "scope", "space", "space_id", "agent", "session"}
_SPACE_VERBS = {
    "create_space",
    "get_space",
    "update_space",
    "archive_space",
    "delete_space",
    "export_space",
    "space_usage",
    "get_space_policy",
    "set_space_policy",
    "list_space_members",
    "add_space_member",
    "remove_space_member",
}

_COMMON_FIELDS = {"target", "trace_id"}

_STRING_FIELDS = {
    "content",
    "query",
    "item_id",
    "job_id",
    "mode",
    "source",
    "modality",
    "owner_ref",
    "approval_token",
    "display_name",
    "status",
    "principal_path",
    "cursor",
    "role",
    "expires_at",
    "occurred_at",
    "stream_id",
    "idempotency_key",
    "uri",
    "trace_id",
    "key",
    "value",
    "memory_type",
}
_LIST_FIELDS = {"tags", "assets", "item_ids", "memory_types", "mem_types", "items"}
_OBJECT_FIELDS = {
    "filters",
    "filter",
    "extensions",
    "system_metadata",
    "user_metadata",
    "policy",
    "metadata",
    "defaults",
}
_BOOL_FIELDS = {"hard", "trace", "continue_on_error", "include_audit"}
_INT_FIELDS = {"k", "limit", "offset", "max_tokens", "sequence"}
_ENUM_FIELDS = {
    "modality": Modality,
    "source": Modality,
    "status": SpaceStatus,
    "principal_path": PrincipalPath,
}

_VERB_FIELDS: dict[str, set[str]] = {
    "add": _COMMON_FIELDS
    | {
        "content",
        "tags",
        "assets",
        "system_metadata",
        "user_metadata",
        "modality",
        "source",
        "owner_ref",
        "uri",
        "extensions",
    },
    "batch_add": _COMMON_FIELDS
    | {
        "defaults",
        "items",
        "continue_on_error",
        "content",
        "tags",
        "assets",
        "system_metadata",
        "user_metadata",
        "modality",
        "source",
        "occurred_at",
        "stream_id",
        "extensions",
    },
    "search": _COMMON_FIELDS | {"query", "k", "filters", "extensions", "max_tokens", "trace"},
    "list": _COMMON_FIELDS
    | {
        "offset",
        "limit",
        "memory_types",
        "mem_types",
        "memory_type",
        "filters",
        "filter",
        "extensions",
    },
    "get": _COMMON_FIELDS | {"item_id"},
    "update": _COMMON_FIELDS | {"item_id", "content", "tags", "system_metadata", "user_metadata"},
    "delete": _COMMON_FIELDS | {"item_id", "hard", "approval_token"},
    "evolve": _COMMON_FIELDS | {"mode"},
    "job": _COMMON_FIELDS | {"job_id"},
    "inspect": _COMMON_FIELDS | {"item_id", "item_ids"},
    "trace": _COMMON_FIELDS | {"item_id"},
    "audit": {"filters", "limit", "extensions", "trace_id"},
    "admin": {"key", "value", "extensions", "trace_id"},
    "grant": _COMMON_FIELDS | {"grantee"},
    "revoke": _COMMON_FIELDS | {"grantee"},
    "create_space": _COMMON_FIELDS | {"display_name", "policy", "metadata"},
    "get_space": _COMMON_FIELDS,
    "list_spaces": _COMMON_FIELDS | {"status", "limit", "cursor"},
    "update_space": _COMMON_FIELDS
    | {"display_name", "status", "principal_path", "policy", "metadata"},
    "archive_space": _COMMON_FIELDS,
    "delete_space": _COMMON_FIELDS | {"mode"},
    "export_space": _COMMON_FIELDS | {"include_audit"},
    "space_usage": _COMMON_FIELDS,
    "get_space_policy": _COMMON_FIELDS,
    "set_space_policy": _COMMON_FIELDS | {"policy"},
    "list_space_members": _COMMON_FIELDS,
    "add_space_member": _COMMON_FIELDS | {"member", "role", "expires_at"},
    "remove_space_member": _COMMON_FIELDS | {"member"},
}


def is_known_verb(verb: str) -> bool:
    """Return whether the HTTP DTO registry knows a dispatch verb."""
    return verb in _VERB_FIELDS


@dataclass(frozen=True)
class TargetScopeDTO:
    """External target Scope using the compatibility field vocabulary."""

    tenant_id: str
    scope: str = ""
    space: str = ""
    agent: str = ""
    session: str = ""

    def to_scope(self) -> Scope:
        """Map the external DTO to the canonical domain Scope."""
        return Scope(
            org=self.tenant_id,
            space=self.space,
            user=self.scope,
            agent=self.agent,
            session=self.session,
        )


@dataclass(frozen=True)
class HttpRequestDTO:
    """Parsed HTTP request ready for the shared dispatch adapter."""

    verb: str
    payload: dict[str, Any]
    target: TargetScopeDTO | None = None
    grantee: TargetScopeDTO | None = None
    member: TargetScopeDTO | None = None
    items: tuple["HttpBatchItemDTO", ...] = ()

    def to_dispatch_request(
        self,
        *,
        actor: Scope,
        request_id: str = "",
        security: RequestSecurityContext | None = None,
    ) -> DispatchRequest:
        """Build the internal request without re-encoding Scope as payload fields."""
        target = self.target.to_scope() if self.target is not None else None
        return DispatchRequest(
            verb=self.verb,
            actor=actor,
            target=target,
            payload=dict(self.payload),
            surface=Surface.HTTP,
            request_id=request_id,
            security=security,
            grantee=self.grantee.to_scope() if self.grantee is not None else None,
            member=self.member.to_scope() if self.member is not None else None,
            batch_items=tuple(
                DispatchBatchItem(
                    target=item.target.to_scope() if item.target is not None else None,
                    payload=dict(item.payload),
                )
                for item in self.items
            ),
        )


@dataclass(frozen=True)
class HttpBatchItemDTO:
    """One HTTP batch item with an optional complete target override."""

    target: TargetScopeDTO | None
    payload: dict[str, Any]


def parse_request(
    verb: str,
    raw: Any,
    *,
    require_target: bool | None = None,
    _batch_item: bool = False,
) -> HttpRequestDTO:
    """Parse one HTTP request with one key pass and strict field checks."""
    if not isinstance(raw, dict):
        raise ValidationError("request body must be a JSON object")
    allowed = _VERB_FIELDS.get(verb)
    if allowed is None:
        # The shared dispatcher owns the 404 response; still reject an unknown
        # verb without attempting to interpret its payload.
        raise ValidationError(f"unknown verb: {verb!r}")

    if _batch_item:
        # These fields belong to BatchWriteItem and are intentionally not
        # accepted by the standalone ``add`` wire format.
        allowed = (allowed - {"uri"}) | {
            "occurred_at",
            "stream_id",
            "sequence",
            "idempotency_key",
        }

    target_raw = raw.get("target")
    target_required = verb not in {"audit", "admin"} if require_target is None else require_target
    target = _parse_target(target_raw, required=target_required)
    _validate_target_for_verb(verb, target)
    output: dict[str, Any] = {}

    for key, value in raw.items():
        if key.startswith("actor_") or (
            key in _RESERVED_IDENTITY_FIELDS and not (verb == "add_space_member" and key == "role")
        ):
            raise ValidationError(f"field {key!r} is reserved for authenticated identity")
        if key not in allowed:
            raise ValidationError(f"unknown field: {key!r}")
        if key in {"target", "grantee", "member", "items"}:
            continue
        _validate_field_type(key, value, verb=verb)
        output[key] = value

    if "source" in output and "modality" in output and output["source"] != output["modality"]:
        raise ValidationError("source and modality must match when both are provided")

    grantee = None
    member = None
    if verb in {"grant", "revoke"}:
        grantee = _parse_nested_scope(raw.get("grantee"), name="grantee")
    elif verb in {"add_space_member", "remove_space_member"}:
        member = _parse_nested_scope(raw.get("member"), name="member")

    items: tuple[HttpBatchItemDTO, ...] = ()
    if verb == "batch_add":
        items = tuple(_parse_batch_items(raw.get("items")))
        if "defaults" in raw:
            output["defaults"] = _parse_defaults(raw["defaults"])

    return HttpRequestDTO(
        verb=verb,
        payload=output,
        target=target,
        grantee=grantee,
        member=member,
        items=items,
    )


def _parse_target(raw: Any, *, required: bool) -> TargetScopeDTO | None:
    if raw is None:
        if required:
            raise ValidationError("missing required field: 'target'")
        return None
    if not isinstance(raw, dict):
        raise ValidationError("target must be an object")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if key.startswith("actor_") or key in _RESERVED_IDENTITY_FIELDS:
            raise ValidationError(f"field target.{key!r} is reserved for authenticated identity")
        if key not in _TARGET_FIELDS:
            raise ValidationError(f"unknown target field: {key!r}")
        if not isinstance(value, str):
            raise ValidationError(f"target.{key} must be a string")
        if not value.strip():
            raise ValidationError(f"target.{key} must be a non-empty string")
        values[key] = value
    if "space" in values and "space_id" in values:
        raise ValidationError("target.space and target.space_id cannot both be provided")
    space = values.get("space", values.get("space_id", ""))
    tenant_id = values.get("tenant_id", "")
    if not tenant_id:
        raise ValidationError("target.tenant_id must be a non-empty string")
    return TargetScopeDTO(
        tenant_id=tenant_id,
        scope=values.get("scope", ""),
        space=space,
        agent=values.get("agent", ""),
        session=values.get("session", ""),
    )


def _validate_target_for_verb(verb: str, target: TargetScopeDTO | None) -> None:
    if target is None:
        return
    if verb == "list_spaces":
        if target.space or _has_scope_dimensions(target):
            raise ValidationError("list_spaces target accepts tenant_id only")
    elif verb in _SPACE_VERBS:
        if not target.space:
            raise ValidationError(f"{verb} target.space must be a non-empty string")
        if _has_scope_dimensions(target):
            raise ValidationError(f"{verb} target accepts tenant_id and space only")


def _has_scope_dimensions(target: TargetScopeDTO) -> bool:
    return any((target.scope, target.agent, target.session))


def _validate_field_type(key: str, value: Any, *, verb: str) -> None:
    if key in _STRING_FIELDS:
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        if not value.strip():
            raise ValidationError(f"{key} must be a non-empty string")
    if key in _LIST_FIELDS and not isinstance(value, list):
        raise ValidationError(f"{key} must be an array")
    if key in {"tags", "assets", "item_ids", "memory_types", "mem_types"} and isinstance(
        value, list
    ):
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValidationError(f"{key} must contain non-empty strings")
    if key in _OBJECT_FIELDS and not isinstance(value, dict):
        raise ValidationError(f"{key} must be an object")
    if key in _BOOL_FIELDS and not isinstance(value, bool):
        raise ValidationError(f"{key} must be a boolean")
    if key in _INT_FIELDS and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValidationError(f"{key} must be an integer")
    if key in {"k", "limit", "max_tokens"} and isinstance(value, int) and value <= 0:
        raise ValidationError(f"{key} must be a positive integer")
    if key in {"offset", "sequence"} and isinstance(value, int) and value < 0:
        raise ValidationError(f"{key} must be a non-negative integer")
    enum_cls = _ENUM_FIELDS.get(key)
    if enum_cls is not None:
        # ``mode`` is verb-specific and handled below because space deletion
        # and memory evolution intentionally use different enum sets.
        try:
            enum_cls(value)
        except ValueError:
            allowed = ", ".join(item.value for item in enum_cls)
            raise ValidationError(f"{key} must be one of: {allowed}") from None
    if key == "mode":
        enum_cls = DeleteMode if verb == "delete_space" else EvolveMode
        try:
            enum_cls(value)
        except ValueError:
            allowed = ", ".join(item.value for item in enum_cls)
            raise ValidationError(f"mode must be one of: {allowed}") from None


def _parse_nested_scope(raw: Any, *, name: str) -> TargetScopeDTO:
    target = _parse_target(raw, required=True)
    if target is None:
        raise ValidationError(f"missing required field: {name!r}")
    if not target.scope:
        raise ValidationError(f"{name}.scope must be a non-empty string")
    return target


def _parse_defaults(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("defaults must be an object")
    if "uri" in raw:
        raise ValidationError("batch_add defaults does not support uri")
    parsed = parse_request("add", raw, require_target=False)
    return parsed.payload


def _parse_batch_items(raw: Any) -> list[HttpBatchItemDTO]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError("items must be a non-empty array")
    result: list[HttpBatchItemDTO] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("each batch item must be an object")
        parsed = parse_request("add", item, require_target=False, _batch_item=True)
        result.append(HttpBatchItemDTO(target=parsed.target, payload=dict(parsed.payload)))
    return result
