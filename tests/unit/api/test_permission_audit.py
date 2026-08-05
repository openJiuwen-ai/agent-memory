from __future__ import annotations

import pytest

from api.memory_api_impl import build_kernel
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import Scope
from config import Config
from construction import EvolveMode
from tests.conftest import root_sec, sec

pytestmark = pytest.mark.unit


def test_permission_denial_is_audited() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    with pytest.raises(PermissionDeniedError):
        api.get("missing", target, security=sec(actor))

    denied = [
        event
        for event in api.audit({"action": "get"}, security=root_sec(), limit=10)
        if event.detail.get("decision") == "deny"
    ]
    assert denied
    assert denied[-1].actor == actor
    assert denied[-1].detail["permission_check"] == "enabled"
    assert "permission_reason" in denied[-1].detail


def test_root_identity_can_use_admin_interfaces_with_sqlite_permission() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api

    assert api.admin_get("rerank.enabled", security=root_sec()) == "true"
    api.admin_set("rerank.enabled", "false", security=root_sec())
    assert api.admin_get("rerank.enabled", security=root_sec()) == "false"


def test_audit_event_view_includes_actor_decision_and_detail_fields() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api
    scope = Scope(org="acme", user="owner")

    api.write("audit event view", scope, security=sec(scope))
    events = api.audit({"action": "write"}, security=root_sec(), limit=10)

    write_event = next(event for event in events if event.action == "write")
    assert write_event.actor == scope
    assert write_event.decision == "allow"
    assert write_event.detail["permission_check"] == "enabled"


def test_evolve_audit_records_job_id_not_unit_id() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    api = build_kernel(config=cfg).api
    scope = Scope(org="acme", user="owner")

    job_id = api.evolve(scope, EvolveMode.EXTRACT, security=sec(scope))
    events = api.audit({"action": "evolve"}, security=root_sec(), limit=10)

    evolve_event = next(event for event in events if event.action == "evolve")
    assert evolve_event.detail["job_id"] == job_id
    assert "after_unit_id" not in evolve_event.detail


def test_configured_sqlite_audit_persists_through_api_audit(tmp_path) -> None:
    db_path = tmp_path / "audit.sqlite3"
    cfg = Config.from_dict(
        {
            "permission": {"default": "sqlite"},
            "audit": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": str(db_path)},
                }
            },
        }
    )
    scope = Scope(org="acme", user="owner")

    first = build_kernel(config=cfg).api
    first.write("persisted audit event", scope, security=sec(scope))

    second = build_kernel(config=cfg).api
    events = second.audit({"action": "write"}, security=root_sec(), limit=10)

    assert any(event.action == "write" and event.actor == scope for event in events)


def test_configured_sqlite_audit_memory_database_is_queryable() -> None:
    cfg = Config.from_dict(
        {
            "permission": {"default": "sqlite"},
            "audit": {
                "default": {
                    "target": "sqlite",
                    "params": {"db_path": ":memory:"},
                }
            },
        }
    )
    scope = Scope(org="acme", user="owner")
    api = build_kernel(config=cfg).api

    api.write("in-memory sqlite audit event", scope, security=sec(scope))
    events = api.audit({"action": "write"}, security=root_sec(), limit=10)

    assert any(event.action == "write" and event.actor == scope for event in events)


def test_require_space_policy_rejects_empty_space_and_audits_denial() -> None:
    cfg = Config.from_dict(
        {
            "permission": {"default": "sqlite"},
            "policy": {
                "default": {
                    "target": "dict",
                    "params": {
                        "policies": {
                            "rerank.enabled": "true",
                            "lifecycle.expired_active.target": "forgotten",
                            "lifecycle.superseded.target": "forgotten",
                            "scope.require_space": "true",
                        }
                    },
                }
            },
        }
    )
    api = build_kernel(config=cfg).api
    scope = Scope(org="acme", user="owner")

    with pytest.raises(ValidationError):
        api.write("missing space", scope, security=sec(scope))

    denied = [
        event
        for event in api.audit({"action": "write"}, security=root_sec(), limit=10)
        if event.decision == "deny"
    ]
    assert denied
    assert denied[-1].detail["permission_reason"] == "scope.space is required"
