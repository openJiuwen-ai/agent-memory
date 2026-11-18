from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    AuthenticationError,
    BackendError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    Scope,
    SpaceSpec,
    ValidationError,
    assemble_runtime,
    internal_context,
)
from jiuwen_memory.api.memory_api_impl.continuity_ops import ContinuityOpsMixin
from jiuwen_memory.common.security.types import Role
from jiuwen_memory_entry.portal.multi_agent import (
    MultiAgentPortal,
    build_multi_agent_portal,
)
from jiuwen_memory_entry.portal.requests import invoke_browser_api
from tests.support.scoped_authenticator import ScopedAuthenticator

PROJECT = Scope(org="demo", space="project")


class _NoContinuityAPI(ContinuityOpsMixin):
    def __init__(self) -> None:
        self._continuity = None

    def _authorize(self, *_args, **_kwargs):
        return {}


def _space_runtime(tmp_path):
    return assemble_runtime(config={
        "engine": {
            "default": {
                "target": "cloud",
                "params": dict.fromkeys((
                    "ingestor", "retriever", "evolver", "index_builder",
                    "classifier", "scheduler", "policy",
                ), "default"),
            },
        },
        "authorizer": {
            "default": {"target": "space_aware", "params": {"delegate": "standard"}},
            "standard": {
                "target": "standard",
                "params": {"grant_store": "default", "delegation_store": "default"},
            },
        },
        "kv_store": {
            "default": {
                "target": "sqlite",
                "params": {"db_path": str(tmp_path / "mem.db"), "fixed_path": True},
            },
            "registry": {
                "target": "sqlite",
                "params": {"db_path": str(tmp_path / "registry.db"), "fixed_path": True},
            },
        },
        "permission": {
            "default": {
                "target": "sqlite",
                "params": {"db_path": str(tmp_path / "permissions.db")},
            },
        },
    })


def portal(scope: dict[str, str] | None = None) -> MultiAgentPortal:
    return build_multi_agent_portal(
        {
            "scope": scope or {"org": "demo", "space": "project"},
            "project_name": "Project",
            "connectors": {"codex": "Codex", "work-swarm": "WorkSwarm"},
        },
    )


def test_real_runtime_without_continuity_keeps_shared_list_available(tmp_path) -> None:
    scope = {"org": "demo", "space": "tenant-a", "user": "owner"}
    actor = Scope(org="demo", user="owner")
    security = internal_context(ScopedAuthenticator(actor, role=Role.ROOT))
    runtime = _space_runtime(tmp_path)
    try:
        runtime.api.create_space(
            SpaceSpec(org="demo", space="tenant-a", owner=actor), security=security,
        )
        configured = portal(scope)

        context = invoke_browser_api(
            runtime.api, "portal_context", {}, security=security, multi_agent=configured,
        )
        listed = invoke_browser_api(
            runtime.api,
            "list",
            {"scope": scope, "offset": 0, "limit": 20},
            security=security,
            multi_agent=configured,
        )
    finally:
        runtime.close()

    assert context["available"] is True
    assert context["mode"] is None
    assert context["role_catalog"]["reason"] == "catalog_unavailable"
    assert listed["items"] == []


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


def test_multi_agent_context_without_continuity_keeps_shared_reads_available() -> None:
    api = _NoContinuityAPI()
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))

    result = invoke_browser_api(
        api, "portal_context", {}, security=security, multi_agent=portal(),
    )

    assert result["available"] is True
    assert result["mode"] is None
    assert result["role_catalog"]["available"] is False
    assert result["role_catalog"]["reason"] == "catalog_unavailable"


def test_multi_agent_context_does_not_hide_catalog_authorization_errors() -> None:
    api = Mock()
    api.continuity_catalog_snapshot.side_effect = PermissionDeniedError("continuity_catalog")
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))

    with pytest.raises(PermissionDeniedError):
        invoke_browser_api(
            api, "portal_context", {}, security=security, multi_agent=portal(),
        )


def test_multi_agent_controls_use_full_scope_and_records_keep_isolation() -> None:
    narrow = {
        "org": "demo", "space": "project", "user": "owner",
        "agent": "worker", "session": "run-1",
    }
    configured = portal(narrow)

    context, context_api = browser({}, verb="portal_context", multi_agent=configured)
    assert context["scope"] == {
        "org": "demo", "space": "project", "user": "owner",
        "agent": "worker", "session": "run-1",
    }
    assert context_api.continuity_catalog_snapshot.call_args.args[0] == Scope(**narrow)

    _tasks, task_api = browser({
        "scope": narrow,
        "request": {"operation": "find", "query": "payment", "top_k": 5},
    }, verb="continuity_task", multi_agent=configured)
    assert task_api.continuity_task.call_args.kwargs["scope"] == Scope(**narrow)
    with pytest.raises(PermissionDeniedError):
        browser({
            "scope": {
                "org": "demo", "space": "project", "user": "owner",
                "agent": "worker", "session": "run-2",
            },
            "request": {"operation": "find", "query": "payment", "top_k": 5},
        }, verb="continuity_task", multi_agent=configured)

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
            {
                "unit_id": "allowed",
                "content": "outside record with a colliding ID",
                "view": {
                    "discovery": "background", "required": True,
                    "evidence_refs": [{**outside_ref, "unit_id": "allowed"}],
                },
            },
        ],
        "paths": [
            {"path_id": "p1", "anchor": allowed_ref, "steps": []},
            {"path_id": "p2", "anchor": outside_ref, "steps": []},
        ],
        "gaps": [{"code": "partial", "message": "partial", "affected_item_ids": ["outside"]}],
        "view_meta": {"anchors": [allowed_ref, outside_ref], "continuation_ready": True},
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
    assert view["view_meta"]["continuation_ready"] is False
    assert view["gaps"][0]["affected_item_ids"] == []
    assert view["gaps"][-1]["code"] == "portal_scope_filtered"
    assert view["gaps"][-1]["blocking"] is True
    api.get.assert_called_once_with("allowed", allowed, security=security)

    task = invoke_browser_api(
        api, "continuity_task", {
            "scope": narrow,
            "request": {"operation": "get", "task_id": "task-a"},
        }, security=security, multi_agent=configured,
    )
    assert task["background"] == [{"ref": allowed_ref, "required": True}]
    assert task["background_partial"] is True


def test_multi_agent_does_not_allow_trace_overrides() -> None:
    api = Mock()
    with pytest.raises(ValidationError, match="trace overrides"):
        invoke_browser_api(
            api, "list", {"scope": vars(PROJECT), "extensions": {"trace": "memory:debug"}},
            security=None, multi_agent=portal(),
        )
    api.list.assert_not_called()


@pytest.mark.parametrize(
    ("error", "filtered"),
    [
        (NotFoundError("record"), True),
        (PermissionDeniedError("get"), False),
        (AuthenticationError("credentials expired"), False),
        (RateLimitedError("request limit"), False),
        (BackendError("record lookup failed"), False),
    ],
)
def test_multi_agent_record_recheck_distinguishes_invisibility_from_failure(
    error, filtered,
) -> None:
    api = Mock()
    api.search.return_value = {"items": [{"unit_id": "record"}], "gaps": []}
    api.get.side_effect = error
    security = SimpleNamespace(auth=SimpleNamespace(actor=Scope(org="demo", user="reader")))
    payload = {
        "query": "timeout", "context": {"scope": vars(PROJECT)}, "duty": "diagnosis",
    }
    if filtered:
        result = invoke_browser_api(
            api, "search", payload, security=security, multi_agent=portal(),
        )
        assert result["items"] == []
        assert [gap["code"] for gap in result["gaps"]] == ["portal_scope_filtered"]
    else:
        with pytest.raises(type(error)) as exc:
            invoke_browser_api(api, "search", payload, security=security, multi_agent=portal())
        assert exc.value is error


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
