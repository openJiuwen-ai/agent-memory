from __future__ import annotations

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

# 平台运维主体：调管理面入口（audit / admin_*）用。必须具名——PR2 起空 Scope 不再是
# 特权形态，判定实现第 2 步直接拒（``empty_actor``）。不带 org 是刻意的：这些入口的
# 目标是系统级资源（跨 org 审计、全局配置），运维主体不隶属某个 org。ROOT 显式传入：
# 过渡件默认给 USER（它有生产调用点，默认 ROOT 会把每个认证请求提到最高权限），
# 而系统级资源的角色闸门只认 ROOT 这一档。
PLATFORM_OPS = Scope(org="system", user="platform-ops")
SEC_OPS = internal_context(ScopedAuthenticator(PLATFORM_OPS, role=Role.ROOT))


def test_permission_denial_is_audited() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api
    actor = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    with pytest.raises(PermissionDeniedError):
        api.get("missing", target, security=internal_context(ScopedAuthenticator(actor)))

    events = api.audit({"action": "get"}, security=SEC_OPS, limit=10)
    denied = [event for event in events if event.detail.get("decision") == "deny"]
    assert denied
    assert denied[-1].actor == actor
    assert denied[-1].detail["permission_check"] == "enabled"
    assert "permission_reason" in denied[-1].detail


def test_root_identity_can_use_admin_interfaces_with_sqlite_permission() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api

    assert api.admin_get("rerank.enabled", security=SEC_OPS) == "true"
    api.admin_set("rerank.enabled", "false", security=SEC_OPS)
    assert api.admin_get("rerank.enabled", security=SEC_OPS) == "false"


def test_audit_event_view_includes_actor_decision_and_detail_fields() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    kernel = build_kernel(config=cfg)
    api = kernel.api
    scope = Scope(org="acme", user="owner")

    api.add("audit event view", scope, security=internal_context(ScopedAuthenticator(scope)))
    events = api.audit({"action": "add"}, security=SEC_OPS, limit=10)

    add_event = next(event for event in events if event.action == "add")
    assert add_event.actor == scope
    assert add_event.decision == "allow"
    assert add_event.detail["permission_check"] == "enabled"


def test_evolve_audit_records_job_id_not_unit_id() -> None:
    cfg = Config.from_dict({"permission": {"default": "sqlite"}})
    api = build_kernel(config=cfg).api
    scope = Scope(org="acme", user="owner")

    job_id = api.evolve(
        scope, EvolveMode.EXTRACT, security=internal_context(ScopedAuthenticator(scope))
    )
    events = api.audit({"action": "evolve"}, security=SEC_OPS, limit=10)

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
    first.add("persisted audit event", scope, security=internal_context(ScopedAuthenticator(scope)))

    second = build_kernel(config=cfg).api
    events = second.audit({"action": "add"}, security=SEC_OPS, limit=10)

    assert any(event.action == "add" and event.actor == scope for event in events)


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

    api.add(
        "in-memory sqlite audit event", scope, security=internal_context(ScopedAuthenticator(scope))
    )
    events = api.audit({"action": "add"}, security=SEC_OPS, limit=10)

    assert any(event.action == "add" and event.actor == scope for event in events)


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
        api.add("missing space", scope, security=internal_context(ScopedAuthenticator(scope)))

    events = api.audit({"action": "add"}, security=SEC_OPS, limit=10)
    denied = [event for event in events if event.decision == "deny"]
    assert denied
    assert denied[-1].detail["permission_reason"] == "scope.space is required"
