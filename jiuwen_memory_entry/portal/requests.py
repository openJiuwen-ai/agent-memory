# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Limit browser credentials to the Portal's subset of the existing HTTP API."""

from dataclasses import replace

from jiuwen_memory.api import (
    ConflictError,
    LifecycleState,
    NotFoundError,
    PermissionDeniedError,
    Scope,
    UnsupportedCapabilityError,
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

EXPERIENCE_VERBS = {"experience_list", "experience_get", "experience_rate"}
BROWSER_VERBS = CONFIG_VERBS | EXPERIENCE_VERBS | {
    "list", "search", "add", "get", "update", "delete",
}
_MAX_EXPERIENCES = 10000


def portal_experience_space(value):
    """Validate the optional scope supplied by the host at launch."""
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValidationError("invalid Portal experience space")
    return value


def _experience_item(unit):
    metadata = unit.system_metadata
    details = {}
    for key in ("category", "condition", "capability", "action"):
        value = metadata.get(key)
        details[key] = value if isinstance(value, str) else ""
    return {
        "id": unit.id,
        "content": unit.content,
        "tier": unit.tier.value,
        "lifecycle": unit.lifecycle.value,
        "source": metadata.get("source_app", ""),
        "agent": unit.scope.agent or metadata.get("author_agent", ""),
        "project": metadata.get("project", ""),
        **details,
        "updated_at": to_jsonable(unit.temporal.t_valid or unit.temporal.t_ingest),
    }


def _is_experience(unit, scope):
    return (
        unit.scope == scope
        and unit.lifecycle is LifecycleState.ACTIVE
        and unit.system_metadata.get("memory_type") == "experience"
    )


def _related_skills(api, scope, entry_ids, security, *, detail=False):
    related = {entry_id: [] for entry_id in entry_ids}
    if not related:
        return related
    offset = 0
    while True:
        page = api.list(
            scope, security=security, memory_types=["skill"], filters={"lifecycle": "active"},
            offset=offset, limit=100,
        )
        for skill in page.items:
            if skill.scope != scope or skill.system_metadata.get("memory_type") != "skill":
                continue
            for entry_id in related.keys() & set(skill.provenance):
                record = {"name": skill.system_metadata.get("skill_name") or skill.id}
                if detail:
                    record["content"] = skill.content
                    record["status"] = next(
                        (s for s in ("approved", "trial", "draft") if s in skill.tags), "",
                    )
                related[entry_id].append(record)
        offset += len(page.items)
        if not page.items or offset >= page.count:
            return related


def _list_experiences(api, payload, scope: Scope, security):
    allowed = {"query", "category", "offset", "limit", "source", "agent", "project", "lifecycle"}
    if payload.keys() - allowed:
        raise ValidationError("unsupported experience list fields")
    offset, limit = payload.get("offset", 0), payload.get("limit", 20)
    query, category = payload.get("query", ""), payload.get("category", "")
    if type(offset) is not int or offset < 0:
        raise ValidationError("invalid experience list options")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("invalid experience list options")
    if not isinstance(query, str) or not isinstance(category, str):
        raise ValidationError("invalid experience list options")
    if len(query) > 500 or len(category) > 128:
        raise ValidationError("invalid experience list options")
    facets = {name: payload.get(name) for name in ("source", "agent", "project", "lifecycle")}
    if any(value is not None and not isinstance(value, str) for value in facets.values()):
        raise ValidationError("invalid experience filters")
    page = api.list(
        scope, security=security, limit=_MAX_EXPERIENCES + 1, memory_types=["experience"],
        filters={"lifecycle": "active"},
    )
    if page.count > _MAX_EXPERIENCES:
        raise UnsupportedCapabilityError("list_size", str(_MAX_EXPERIENCES), "portal experience")
    if len(page.items) != page.count:
        raise ConflictError(message="could not obtain a complete experience list")
    items = [_experience_item(unit) for unit in page.items if _is_experience(unit, scope)]
    categories = sorted({item["category"] for item in items if item["category"]})
    needle = query.strip().casefold()
    matches = []
    for item in items:
        if category and item["category"] != category:
            continue
        if needle not in item["content"].casefold():
            continue
        if any(value is not None and item[name] != value for name, value in facets.items()):
            continue
        matches.append(item)
    matches.sort(key=lambda item: (item["updated_at"] or "", item["id"]), reverse=True)
    visible = matches[offset:offset + limit]
    skills = _related_skills(api, scope, [item["id"] for item in visible], security)
    for item in visible:
        item["has_skills"] = bool(skills[item["id"]])
        try:
            _, feedback = api.experience_feedback(item["id"], scope, security=security)
        except (PermissionDeniedError, NotFoundError):
            continue
        item["counts"] = feedback["counts"]
    return {
        "available": True, "items": visible,
        "count": len(matches), "categories": categories,
        "sources": sorted({item["source"] for item in items if item["source"]}),
        "agents": [[a, a] for a in sorted({item["agent"] for item in items if item["agent"]})],
        "projects": sorted({item["project"] for item in items if item["project"]}),
    }


def _read_experience(api, verb, payload, security, experience_space):
    if not experience_space:
        raise NotFoundError("portal_experience_space")
    if not isinstance(payload, dict):
        raise ValidationError("experience request must be an object")
    scope = Scope(org=security.auth.actor.org, space=experience_space)
    if verb == "experience_list":
        return _list_experiences(api, payload, scope, security)
    unit_id = payload.get("unit_id")
    rating = verb == "experience_rate"
    fields = {"unit_id", "choice"} if rating else {"unit_id"}
    if payload.keys() != fields or not isinstance(unit_id, str) or not unit_id:
        raise ValidationError("invalid experience detail or rating fields")
    choice = payload.get("choice", "read")
    if rating and choice not in ("helpful", "neutral", "harmful", "clear"):
        raise ValidationError("invalid personal experience feedback")
    unit, feedback = api.experience_feedback(unit_id, scope, security=security, choice=choice)
    if rating:
        return feedback
    parents = []
    for source_id in dict.fromkeys(unit.provenance):
        try:
            source = api.get(source_id, scope, security=security)
        except (NotFoundError, PermissionDeniedError):
            continue
        if source.scope == scope and source.system_metadata.get("memory_type") == "experience":
            parents.append({"id": source.id, "content": source.content})
    return {
        **_experience_item(unit), "feedback": feedback,
        "sources": api.experience_task_sources(unit_id, scope, security=security),
        "parents": parents,
        "skills": _related_skills(api, scope, [unit_id], security, detail=True)[unit_id],
    }


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
    if unit.system_metadata.get("memory_type") == "experience":
        raise PermissionDeniedError(message="Portal experience is read-only")
    if unit.lifecycle is not LifecycleState.ACTIVE:
        raise ConflictError(message="memory is no longer active")


def invoke_browser_api(
    api, verb, payload, *, security, write_scope=None, experience_space=None, configuration=None,
):
    """Apply browser request restrictions before calling the public memory API."""
    if verb not in BROWSER_VERBS:
        raise ValidationError("unsupported browser API")
    if verb in CONFIG_VERBS:
        return invoke_configuration(configuration, verb, payload, security)
    if verb in EXPERIENCE_VERBS:
        return _read_experience(api, verb, payload, security, experience_space)


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
