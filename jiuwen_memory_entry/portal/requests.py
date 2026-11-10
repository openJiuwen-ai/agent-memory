# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Limit browser credentials to the Portal's subset of the existing HTTP API."""

from dataclasses import replace

from jiuwen_memory.api import (
    ConflictError,
    LifecycleState,
    PermissionDeniedError,
    ValidationError,
    read_personal_record,
)
from jiuwen_memory_entry.core.api_contract import (
    CONFIG_VERBS,
    invoke_api,
    invoke_configuration,
    parse_request,
    to_jsonable,
)

BROWSER_VERBS = CONFIG_VERBS | {"list", "search", "add", "get", "update", "delete"}


def _label(value, name):
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValidationError(f"invalid memory {name}")
    return value.strip()


def _content(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("content must be a non-empty string")
    return value.strip()


def _add_scope(actor, write_scope, agent):
    if not actor.org or not actor.user:
        raise PermissionDeniedError(message="personal writes require a user identity")
    scope = write_scope if write_scope is not None else actor
    if (scope.org, scope.user) != (actor.org, actor.user):
        raise PermissionDeniedError(message="write scope is outside the authenticated user")
    if write_scope is not None and agent is not None and agent != scope.agent:
        raise PermissionDeniedError(message="write agent is fixed by the host")
    return replace(scope, agent=scope.agent if agent is None else agent)


def _require_active_record(api, scope, unit_id, security):
    unit = read_personal_record(api, scope, unit_id, security=security)
    if unit.lifecycle is not LifecycleState.ACTIVE:
        raise ConflictError(message="memory is no longer active")


def invoke_browser_api(api, verb, payload, *, security, write_scope=None, configuration=None):
    """Apply browser request restrictions before calling the public memory API."""
    if verb not in BROWSER_VERBS:
        raise ValidationError("unsupported browser API")
    if verb in CONFIG_VERBS:
        return invoke_configuration(configuration, verb, payload, security)

    if verb == "list":
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("extensions"), dict)
            or "personal" not in payload["extensions"]
        ):
            raise ValidationError("browser list requires personal options")
        result = invoke_api(api, verb, payload, security)
        scope = write_scope if write_scope is not None else security.auth.actor
        result["add_context"] = {"agent": scope.agent, "agent_locked": write_scope is not None}
        return result
    if verb == "search":
        arguments = parse_request(verb, payload)
        if "personal" not in arguments["context"].extensions:
            raise ValidationError("browser search requires personal options")
        return to_jsonable(api.search(**arguments, security=security))

    arguments = parse_request(verb, payload)
    if verb == "add":
        scope = payload["scope"]
        metadata = payload.get("system_metadata", {})
        valid_fields = payload.keys() <= {"content", "scope", "system_metadata"}
        valid_metadata = isinstance(metadata, dict) and metadata.keys() <= {"project"}
        if not valid_fields or scope.keys() - {"agent"} or not valid_metadata:
            raise ValidationError("unsupported memory fields")
        agent = _label(scope["agent"], "agent") if "agent" in scope else None
        project = _label(metadata.get("project", ""), "project")
        if project in {".", ".."} or "/" in project or "\\" in project:
            raise ValidationError("project must be a single directory name")
        target = _add_scope(security.auth.actor, write_scope, agent)
        system_metadata = {"source_app": "portal"}
        if project:
            system_metadata["project"] = project
        return to_jsonable(api.add(
            _content(arguments["content"]), target,
            security=security, system_metadata=system_metadata,
        ))
    if verb == "delete":
        selector = arguments["selector"]
        single_target = (
            selector.scope is not None and len(selector.unit_ids) == 1
            and bool(selector.unit_ids[0])
        )
        if (
            set(payload["selector"]) != {"scope", "unit_ids", "mode"}
            or not single_target
            or selector.mode.value != "forget"
        ):
            raise ValidationError("browser delete requires one explicit forget target")
        _require_active_record(api, selector.scope, selector.unit_ids[0], security)
        return to_jsonable(api.delete(selector, security=security))

    scope, unit_id = arguments["scope"], arguments["unit_id"]
    if verb == "get":
        if "as_of" in payload:
            raise ValidationError("browser get requires an exact record")
        return to_jsonable(read_personal_record(api, scope, unit_id, security=security))
    if set(payload["patch"]) != {"content"}:
        raise ValidationError("browser update only accepts content")
    patch = arguments["patch"]
    patch.content = _content(patch.content)
    _require_active_record(api, scope, unit_id, security)
    return to_jsonable(api.update(unit_id, scope, patch, security=security))
