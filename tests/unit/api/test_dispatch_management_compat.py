from __future__ import annotations

import importlib
import os
import sys

import pytest

from common.type_def import Segment
from control import PrincipalPath, SpaceInfo, SpaceStatus

pytestmark = pytest.mark.unit

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "bootstrap",
    "core",
)
if _BOOTSTRAP not in sys.path:
    sys.path.append(_BOOTSTRAP)

handler = importlib.import_module("handler")
profiles = importlib.import_module("profiles")
server = importlib.import_module("server")
OFFLINE = profiles.OFFLINE
load_config = profiles.load_config
Server = server.Server


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
    assert body["grantee"]["user"] == "reader"


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
                    target=handler.Scope(org="acme", space="coding", user="owner"),
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
        {
            "action": "write",
            "decision": "allow",
            "actor_user": "owner",
            "target_space": "coding",
        },
    )

    assert status == 200
    assert srv.api.filters == {
        "action": "write",
        "decision": "allow",
        "actor_user": "owner",
        "target_space": "coding",
    }
    assert body["count"] == 1
    assert body["events"][0]["actor"]["user"] == "owner"
    assert body["events"][0]["target"]["space"] == "coding"


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


def test_dispatch_list_delegates_to_api_with_pagination_and_type_filter() -> None:
    class _Api:
        def __init__(self) -> None:
            self.call = None

        def list(
            self,
            scope,
            *,
            identity,
            offset=0,
            limit=100,
            memory_types=None,
        ):
            self.call = {
                "scope": scope,
                "identity": identity,
                "offset": offset,
                "limit": limit,
                "memory_types": memory_types,
            }
            return [
                handler.MemoryUnit(
                    id="unit-1",
                    scope=scope,
                    segments=[Segment(content="repo uses pytest")],
                    metadata={"memory_type": "coding"},
                )
            ]

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()
    status, body = handler.dispatch(
        srv,
        "list",
        {
            "tenant_id": "acme",
            "scope": "owner",
            "actor_scope": "reader",
            "offset": "2",
            "limit": "5",
            "memory_types": "coding,episodic",
        },
    )

    assert status == 200
    assert srv.api.call == {
        "scope": handler.Scope(org="acme", user="owner"),
        "identity": handler.Scope(org="acme", user="reader"),
        "offset": 2,
        "limit": 5,
        "memory_types": ["coding", "episodic"],
    }
    assert body["ok"] is True
    assert body["op"] == "list"
    assert body["count"] == 1
    assert body["offset"] == 2
    assert body["limit"] == 5
    assert body["items"][0]["item_id"] == "unit-1"


@pytest.mark.parametrize("payload", [{"offset": -1}, {"limit": 0}, {"memory_types": {}}])
def test_dispatch_list_rejects_invalid_options(payload) -> None:
    class _Api:
        @staticmethod
        def list(*args, **kwargs):
            raise AssertionError("list should not be called with invalid options")

    class _Srv:
        api = _Api()

    status, body = handler.dispatch(
        _Srv(),
        "list",
        {"tenant_id": "acme", "scope": "owner", **payload},
    )

    assert status == 400
    assert body["error"] == "ValidationError"


def test_dispatch_create_space_delegates_to_api_with_space_spec() -> None:
    class _Api:
        def __init__(self) -> None:
            self.call = None

        def create_space(self, spec, *, identity):
            self.call = {"spec": spec, "identity": identity}
            return SpaceInfo(
                org=spec.org,
                space=spec.space,
                display_name=spec.display_name,
                status=SpaceStatus.ACTIVE,
                principal_path=spec.principal_path,
                policy=spec.policy,
                metadata=spec.metadata,
            )

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()
    status, body = handler.dispatch(
        srv,
        "create_space",
        {
            "tenant_id": "acme",
            "space": "coding",
            "actor_space": "",
            "actor_scope": "",
            "display_name": "Coding",
            "principal_path": "agent_user",
            "policy": {"pipeline_profiles": {"coding": "coding"}},
            "metadata": {"env": "prod"},
        },
    )

    assert status == 200
    assert srv.api.call["identity"] == handler.Scope(org="acme")
    assert srv.api.call["spec"].org == "acme"
    assert srv.api.call["spec"].space == "coding"
    assert srv.api.call["spec"].principal_path == PrincipalPath.AGENT_USER
    assert srv.api.call["spec"].policy.pipeline_profiles == {"coding": "coding"}
    assert body["space"]["metadata"] == {"env": "prod"}
