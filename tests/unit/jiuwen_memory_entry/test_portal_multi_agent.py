from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import PermissionDeniedError, Scope, ValidationError
from jiuwen_memory_entry.portal.multi_agent import (
    MultiAgentPortal,
    build_multi_agent_portal,
)
from jiuwen_memory_entry.portal.requests import invoke_browser_api

PROJECT = Scope(org="demo", space="project")


def portal(scope: dict[str, str] | None = None) -> MultiAgentPortal:
    return build_multi_agent_portal(
        {
            "scope": scope or {"org": "demo", "space": "project"},
            "project_name": "Project",
            "connectors": {"codex": "Codex", "work-swarm": "WorkSwarm"},
        },
    )


def browser(payload=None, *, verb: str = "list", multi_agent=None):
    api = Mock()
    api.list.return_value = {"items": [], "count": 0}
    api.search.return_value = {"items": [], "paths": [], "gaps": [], "errors": []}
    api.continuity_task.return_value = {"tasks": []}
    api.get.return_value = {"id": "one"}
    api.continuity_catalog_snapshot.return_value = {
        "mode": "role",
        "role_catalog": {
            "available": True,
            "reason": "",
            "policy_version": "policy-v1",
            "default_duty": "general",
            "duties": [{
                "id": "general",
                "display_name": "综合",
                "description": "",
                "traversal": [],
                "budget": {},
            }],
            "relation_types": [],
        },
    }
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))
    arguments = payload if payload is not None else {
        "scope": {"org": "demo", "space": "project", "user": "", "agent": "", "session": ""},
    }
    return invoke_browser_api(
        api, verb, arguments, security=security, multi_agent=multi_agent or portal(),
    ), api


def test_multi_agent_capability_is_the_only_page_context_read() -> None:
    result, api = browser({}, verb="portal_context")
    assert result["available"] is True
    assert result["scope"] == {
        "org": "demo", "space": "project", "user": "", "agent": "", "session": "",
    }
    assert result["connectors"] == [
        {"id": "codex", "label": "Codex"},
        {"id": "work-swarm", "label": "WorkSwarm"},
    ]
    assert result["mode"] == "role"
    assert result["role_catalog"]["policy_version"] == "policy-v1"
    assert result["role_catalog"]["duties"][0]["id"] == "general"
    assert [entry[0] for entry in api.method_calls] == ["continuity_catalog_snapshot"]


def test_multi_agent_mode_comes_from_runtime_not_portal_config() -> None:
    configured = portal()
    assert configured.capability()["mode"] is None

    result, _api = browser({}, verb="portal_context", multi_agent=configured)
    assert result["mode"] == "role"


def test_multi_agent_controls_use_space_scope_and_records_keep_narrowing() -> None:
    narrow = {"org": "demo", "space": "project", "agent": "worker"}
    configured = portal(narrow)

    context, context_api = browser({}, verb="portal_context", multi_agent=configured)
    assert context["scope"] == {
        "org": "demo", "space": "project", "user": "", "agent": "worker", "session": "",
    }
    assert context_api.continuity_catalog_snapshot.call_args.args[0] == PROJECT

    for task_scope in (narrow, {"org": "demo", "space": "project"}):
        _tasks, task_api = browser({
            "scope": task_scope,
            "request": {"operation": "find", "query": "payment", "top_k": 5},
        }, verb="continuity_task", multi_agent=configured)
        assert task_api.continuity_task.call_args.kwargs["scope"] == PROJECT

    _records, record_api = browser({
        "query": "timeout",
        "context": {"scope": narrow},
        "duty": "diagnosis",
    }, verb="search", multi_agent=configured)
    assert record_api.search.call_args.kwargs["context"].scope == Scope(**narrow)


def test_multi_agent_filters_indirect_references_outside_bound_record_scope() -> None:
    narrow = {"org": "demo", "space": "project", "agent": "worker"}
    configured = portal(narrow)
    allowed = Scope(**narrow)
    outside = Scope(org="demo", space="project", agent="worker-other")
    allowed_ref = {"scope": vars(allowed), "unit_id": "allowed"}
    outside_ref = {"scope": vars(outside), "unit_id": "outside"}
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))
    api = Mock()

    def get_unit(unit_id, _scope, **_kwargs):
        return SimpleNamespace(scope=allowed if unit_id == "allowed" else outside)

    api.get.side_effect = get_unit
    api.search.return_value = {
        "items": [
            {
                "unit_id": "allowed",
                "view": {"evidence_refs": [allowed_ref], "path_ids": ["p1", "p2"]},
            },
            {"unit_id": "outside", "view": {"evidence_refs": [outside_ref], "path_ids": []}},
        ],
        "paths": [
            {"path_id": "p1", "anchor": allowed_ref, "steps": []},
            {"path_id": "p2", "anchor": outside_ref, "steps": []},
        ],
        "gaps": [{"code": "partial", "message": "partial", "affected_item_ids": ["outside"]}],
        "view_meta": {"anchors": [allowed_ref, outside_ref]},
    }
    api.continuity_task.return_value = {
        "background": [
            {"ref": allowed_ref, "required": True},
            {"ref": outside_ref, "required": False},
        ],
        "background_partial": False,
    }

    view = invoke_browser_api(
        api, "search", {
            "query": "timeout", "context": {"scope": narrow}, "duty": "diagnosis",
        }, security=security, multi_agent=configured,
    )
    assert [item["unit_id"] for item in view["items"]] == ["allowed"]
    assert [path["path_id"] for path in view["paths"]] == ["p1"]
    assert view["items"][0]["view"]["path_ids"] == ["p1"]
    assert view["view_meta"]["anchors"] == [allowed_ref]
    assert view["gaps"][0]["affected_item_ids"] == []
    assert view["gaps"][-1]["code"] == "portal_scope_filtered"

    task = invoke_browser_api(
        api, "continuity_task", {
            "scope": narrow,
            "request": {"operation": "get", "task_id": "task-a"},
        }, security=security, multi_agent=configured,
    )
    assert task["background"] == [{"ref": allowed_ref, "required": True}]
    assert task["background_partial"] is True


def test_disabled_multi_agent_context_does_not_inspect_the_runtime() -> None:
    api = Mock()
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))
    disabled = build_multi_agent_portal({"enabled": False})
    result = invoke_browser_api(
        api, "portal_context", {}, security=security, multi_agent=disabled,
    )
    assert result["available"] is False
    assert result["role_catalog"] == {
        "available": False,
        "reason": "multi_agent_unavailable",
        "policy_version": None,
        "default_duty": None,
        "duties": [],
        "relation_types": [],
    }
    assert not api.method_calls


def test_multi_agent_surface_is_scoped_and_read_only() -> None:
    result, api = browser({
        "scope": {"org": "demo", "space": "project"},
        "filters": {"system_metadata.continuity_task": {"eq": "task-a"}},
        "offset": 0,
        "limit": 20,
    })
    assert result == {"items": [], "count": 0}
    api.list.assert_called_once()

    search, search_api = browser({
        "query": "timeout",
        "context": {"scope": {"org": "demo", "space": "project"}},
        "duty": "diagnosis",
        "top_k": 20,
    }, verb="search")
    assert search["items"] == []
    search_api.search.assert_called_once()

    find, find_api = browser({
        "scope": {"org": "demo", "space": "project"},
        "request": {"operation": "find", "query": "payment", "top_k": 5},
    }, verb="continuity_task")
    assert find == {"tasks": []}
    find_api.continuity_task.assert_called_once()

    with pytest.raises(PermissionDeniedError):
        browser({
            "scope": {"org": "other", "space": "project"},
        })
    with pytest.raises(ValidationError):
        browser({
            "scope": {"org": "demo", "space": "project"},
            "extensions": {"personal": {}},
        })
    with pytest.raises(ValidationError):
        browser({
            "scope": {"org": "demo", "space": "project"},
            "request": {
                "operation": "create", "request_id": "request-a", "title": "not allowed",
            },
        }, verb="continuity_task")
    with pytest.raises(ValidationError):
        browser({"scope": {"org": "demo", "space": "project"}}, verb="add")
