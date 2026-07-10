from __future__ import annotations

import os
import sys

import pytest

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "bootstrap",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

import handler  # type: ignore  # noqa: E402
from profiles import OFFLINE, load_config  # type: ignore  # noqa: E402
from server import Server  # type: ignore  # noqa: E402


def test_dispatch_admin_requires_platform_admin_under_default_kernel() -> None:
    srv = Server.build(load_config([OFFLINE]))
    status, body = srv.dispatch("admin", {"tenant_id": "acme", "scope": "alice"})

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_admin_rejects_missing_identity_fields() -> None:
    srv = Server.build(load_config([OFFLINE]))
    payload = {}

    status, body = srv.dispatch("admin", payload)

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_revoke_supports_scope_owner() -> None:
    srv = Server.build(load_config([OFFLINE]))

    status, body = srv.dispatch(
        "revoke",
        {"tenant_id": "acme", "scope": "owner", "grantee": "reader"},
    )

    assert status == 200
    assert body["grantee"] == "reader"


@pytest.mark.parametrize(
    "actor_override",
    [
        {"actor_tenant_id": ""},
        {"actor_tenant_id": "acme", "actor_scope": "outsider"},
    ],
)
def test_dispatch_revoke_rejects_non_owner_actor_overrides(
    actor_override: dict[str, str],
) -> None:
    srv = Server.build(load_config([OFFLINE]))

    status, body = srv.dispatch(
        "revoke",
        {
            "tenant_id": "acme",
            "scope": "owner",
            "grantee": "reader",
            **actor_override,
        },
    )

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_audit_forwards_structured_filters() -> None:
    class _Api:
        def __init__(self) -> None:
            self.filters = None

        def audit(self, filters, *, identity, limit=100):
            self.filters = filters
            return [
                handler.AuditEvent(
                    actor=handler.Scope(org="acme", user="owner"),
                    action="write",
                    decision="allow",
                )
            ]

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()

    status, body = handler.dispatch(
        srv,
        "audit",
        {"action": "write", "decision": "allow", "actor_user": "owner"},
    )

    assert status == 200
    assert srv.api.filters == {
        "action": "write",
        "decision": "allow",
        "actor_user": "owner",
    }
    assert body["count"] == 1
    assert body["events"][0]["actor"]["user"] == "owner"


@pytest.mark.parametrize("limit", ["not-a-number", "", [], -1, 0])
def test_dispatch_audit_rejects_invalid_limit(limit) -> None:
    class _Api:
        @staticmethod
        def audit(filters, *, identity, limit=100):
            raise AssertionError("audit should not be called with an invalid limit")

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    status, body = handler.dispatch(_Srv(), "audit", {"limit": limit})

    assert status == 400
    assert body["error"] == "ValidationError"


def test_dispatch_list_reports_not_implemented() -> None:
    class _Srv:
        pass

    status, body = handler.dispatch(_Srv(), "list", {"tenant_id": "acme", "scope": "owner"})

    assert status == 200
    assert body == {
        "ok": False,
        "op": "list",
        "error": "NotImplemented",
        "message": "list is not yet available",
    }
