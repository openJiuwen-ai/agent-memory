# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Limit browser credentials to the Portal's subset of the existing HTTP API."""

from dataclasses import replace

from jiuwen_memory.api import (
    AgentMemoryError,
    ConflictError,
    LifecycleState,
    PermissionDeniedError,
    Scope,
    ValidationError,
    read_continuity_catalog,
    read_personal_record,
)
from jiuwen_memory_entry.core.api_contract import (
    CONFIG_VERBS,
    invoke_api,
    invoke_configuration,
    parse_request,
    to_jsonable,
)
from jiuwen_memory_entry.portal.multi_agent import MultiAgentPortal, require_multi_agent

BROWSER_VERBS = CONFIG_VERBS | {"list", "search", "add", "get", "update", "delete"}
MULTI_AGENT_VERBS = frozenset({"list", "search", "get", "continuity_task"})
PORTAL_VERBS = BROWSER_VERBS | MULTI_AGENT_VERBS | {"portal_context"}


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


def _reference_scope(reference):
    if not isinstance(reference, dict):
        return None
    raw_scope = reference.get("scope")
    if not isinstance(raw_scope, dict):
        return None
    try:
        return Scope(**raw_scope)
    except (TypeError, ValidationError):
        return None


def _portal_reference_allowed(portal: MultiAgentPortal, reference) -> bool:
    scope = _reference_scope(reference)
    return scope is not None and portal.allows_scope(scope)


def _portal_record_allowed(api, portal, item, security) -> bool:
    unit_id = item.get("unit_id") if isinstance(item, dict) else None
    if not isinstance(unit_id, str) or not unit_id:
        return False
    try:
        unit = api.get(unit_id, portal.scope, security=security)
    except AgentMemoryError:
        return False
    return portal.allows_scope(unit.scope)


def _constrain_task_result(result, portal: MultiAgentPortal):
    if not isinstance(result, dict) or not isinstance(result.get("background"), list):
        return result
    visible = [
        item for item in result["background"]
        if _portal_reference_allowed(portal, item.get("ref") if isinstance(item, dict) else None)
    ]
    if len(visible) != len(result["background"]):
        result["background"] = visible
        result["background_partial"] = True
    return result


def _path_references(path):
    if not isinstance(path, dict):
        return []
    references = [path.get("anchor")]
    for step in path.get("steps") or []:
        if not isinstance(step, dict):
            continue
        references.extend((step.get("source"), step.get("target")))
        references.extend(step.get("evidence_refs") or [])
    return [reference for reference in references if reference is not None]


def _constrain_view_result(api, result, portal: MultiAgentPortal, security):
    if not isinstance(result, dict):
        return result
    filtered = False
    items = []
    for item in result.get("items") or []:
        if not _portal_record_allowed(api, portal, item, security):
            filtered = True
            continue
        view = item.get("view")
        if isinstance(view, dict) and isinstance(view.get("evidence_refs"), list):
            evidence_refs = [
                reference for reference in view["evidence_refs"]
                if _portal_reference_allowed(portal, reference)
            ]
            filtered = filtered or len(evidence_refs) != len(view["evidence_refs"])
            view["evidence_refs"] = evidence_refs
        items.append(item)
    result["items"] = items

    paths = []
    for path in result.get("paths") or []:
        references = _path_references(path)
        if any(not _portal_reference_allowed(portal, ref) for ref in references):
            filtered = True
            continue
        paths.append(path)
    result["paths"] = paths
    allowed_path_ids = {
        path.get("path_id") for path in paths
        if isinstance(path, dict) and isinstance(path.get("path_id"), str)
    }
    for item in items:
        view = item.get("view")
        if isinstance(view, dict) and isinstance(view.get("path_ids"), list):
            view["path_ids"] = [
                path_id for path_id in view["path_ids"] if path_id in allowed_path_ids
            ]

    meta = result.get("view_meta")
    if isinstance(meta, dict) and isinstance(meta.get("anchors"), list):
        anchors = [
            reference for reference in meta["anchors"]
            if _portal_reference_allowed(portal, reference)
        ]
        filtered = filtered or len(anchors) != len(meta["anchors"])
        meta["anchors"] = anchors

    gaps = result.get("gaps")
    if filtered and isinstance(gaps, list):
        for gap in gaps:
            if isinstance(gap, dict):
                gap["affected_item_ids"] = []
        gaps.append({
            "code": "portal_scope_filtered",
            "message": "references outside the Portal scope were omitted",
            "blocking": False,
            "affected_item_ids": [],
        })
    return result


def _multi_agent_arguments(portal: MultiAgentPortal, verb: str, payload):
    """Validate one read-only multi-Agent browser request."""
    portal = require_multi_agent(portal)
    if verb not in MULTI_AGENT_VERBS:
        raise ValidationError("multi-agent Portal is read-only")
    arguments = parse_request(verb, payload)
    if verb == "continuity_task":
        scope = arguments["scope"]
        request = arguments["request"]
        if not portal.allows_space(scope):
            raise PermissionDeniedError(message="task scope is outside the Portal space")
        if request.operation not in {"find", "get"}:
            raise ValidationError("multi-agent Portal only allows task find and get")
        if request.operation == "find":
            if not (request.query or "").strip():
                raise ValidationError("task lookup requires a non-empty query")
            if request.connector_id or request.context_id:
                raise ValidationError("Portal task lookup only accepts query and top_k")
        elif not request.task_id.strip():
            raise ValidationError("task get requires task_id")
        return arguments

    scope = arguments.get("scope")
    if scope is None and verb == "search":
        scope = arguments["context"].scope
    if scope is None or not portal.allows_scope(scope):
        raise PermissionDeniedError(message="scope is outside the Portal space")
    if verb == "list":
        extensions = arguments.get("extensions") or {}
        if {"personal", "spaces"} & set(extensions):
            raise ValidationError("multi-agent list cannot use personal or cross-space options")
    if verb == "search":
        context = arguments["context"]
        if {"personal", "spaces"} & set(context.extensions):
            raise ValidationError("multi-agent search cannot use personal or cross-space options")
        anchor = arguments.get("anchor")
        if anchor is not None and not portal.allows_scope(anchor.scope):
            raise PermissionDeniedError(message="anchor is outside the Portal space")
        if not any((
            arguments["query"].strip(), arguments.get("task_id"), anchor,
            arguments.get("duty"),
        )):
            raise ValidationError("search requires a query, task, anchor or duty")
    if verb == "get" and "as_of" in payload:
        raise ValidationError("multi-agent get requires an exact record")
    return arguments


def invoke_browser_api(
    api, verb, payload, *, security, write_scope=None, configuration=None,
    multi_agent: MultiAgentPortal | None = None,
):
    """Apply browser request restrictions before calling the public memory API."""
    if verb == "portal_context":
        if payload not in ({}, None):
            raise ValidationError("portal_context does not accept fields")
        if multi_agent is None:
            raise ValidationError("multi-agent Portal is not configured")
        capability = multi_agent.capability()
        if not capability["available"]:
            capability["role_catalog"] = {
                "available": False,
                "reason": "multi_agent_unavailable",
                "policy_version": None,
                "default_duty": None,
                "duties": [],
                "relation_types": [],
            }
            return to_jsonable(capability)
        catalog = read_continuity_catalog(
            api, multi_agent.space_scope(), security=security,
        )
        if catalog["mode"] is not None:
            capability["mode"] = catalog["mode"]
        capability["role_catalog"] = catalog["role_catalog"]
        return to_jsonable(capability)
    if multi_agent is not None:
        require_multi_agent(multi_agent)
        arguments = _multi_agent_arguments(multi_agent, verb, payload)
        if verb == "continuity_task":
            payload = {**payload, "scope": to_jsonable(multi_agent.space_scope())}
        result = invoke_api(api, verb, payload, security)
        if verb == "continuity_task":
            result = _constrain_task_result(result, multi_agent)
        elif verb == "search" and any((
            arguments.get("task_id"), arguments.get("anchor"), arguments.get("duty"),
        )):
            result = _constrain_view_result(api, result, multi_agent, security)
        return to_jsonable(result)
    if verb not in BROWSER_VERBS:
        raise ValidationError("unsupported browser API")
    if verb in CONFIG_VERBS:
        return invoke_configuration(configuration, verb, payload, security)

    if verb == "list":
        extensions = payload.get("extensions") if isinstance(payload, dict) else None
        if not isinstance(extensions, dict) or "personal" not in extensions:
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
        raw_selector = payload.get("selector")
        single_target = (
            selector.scope is not None and len(selector.unit_ids) == 1
            and bool(selector.unit_ids[0])
        )
        selector_fields_valid = (
            isinstance(raw_selector, dict)
            and set(raw_selector) == {"scope", "unit_ids", "mode"}
        )
        if not selector_fields_valid or not single_target or selector.mode.value != "forget":
            raise ValidationError("browser delete requires one explicit forget target")
        _require_active_record(api, selector.scope, selector.unit_ids[0], security)
        return to_jsonable(api.delete(selector, security=security))

    scope, unit_id = arguments["scope"], arguments["unit_id"]
    if verb == "get":
        if "as_of" in payload:
            raise ValidationError("browser get requires an exact record")
        return to_jsonable(read_personal_record(api, scope, unit_id, security=security))
    raw_patch = payload.get("patch")
    if not isinstance(raw_patch, dict) or set(raw_patch) != {"content"}:
        raise ValidationError("browser update only accepts content")
    patch = arguments["patch"]
    patch.content = _content(patch.content)
    _require_active_record(api, scope, unit_id, security)
    return to_jsonable(api.update(unit_id, scope, patch, security=security))
