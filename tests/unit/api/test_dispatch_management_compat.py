# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.errors import AuthenticationError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import Scope, Segment
from jiuwen_memory.control import MemoryListResult, PrincipalPath, SpaceInfo, SpaceStatus
from jiuwen_memory_entry.core.dispatch_request import DispatchRequest
from jiuwen_memory_entry.core.legacy_request_adapter import (
    build_legacy_dispatch_request,
)
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit

_BOOTSTRAP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "jiuwen_memory_entry",
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


def _security(actor: Scope):
    """测试布置受控身份：actor 由 security 携带，payload 只描述业务 target。"""
    return internal_context(ScopedAuthenticator(actor))


def _dispatch(srv, verb: str, payload: dict, *, actor: Scope | None = None):
    security = _security(actor if actor is not None else Scope(org="local", user="developer"))
    return handler.dispatch(srv, build_legacy_dispatch_request(verb, payload, security=security))


def test_dispatch_admin_requires_platform_admin_under_default_kernel() -> None:
    srv = Server.build(load_config([OFFLINE]))
    status, body = srv.dispatch(
        "admin",
        {"tenant_id": "acme", "scope": "alice"},
        security=_security(Scope(org="acme", user="alice")),
    )

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_admin_rejects_missing_identity_fields() -> None:
    srv = Server.build(load_config([OFFLINE]))
    payload = {}

    with pytest.raises(AuthenticationError):
        srv.dispatch("admin", payload, security=_security(Scope()))


def test_dispatch_revoke_supports_scope_owner() -> None:
    srv = Server.build(load_config([OFFLINE]))

    _, granted = srv.dispatch(
        "grant",
        {"tenant_id": "acme", "scope": "owner", "grantee": "reader"},
        security=_security(Scope(org="acme", user="owner")),
    )
    status, body = srv.dispatch(
        "revoke",
        {
            "tenant_id": "acme",
            "scope": "owner",
            "grantee": "reader",
            "grant_id": granted["grant_id"],
        },
        security=_security(Scope(org="acme", user="owner")),
    )

    assert status == 200
    assert body["grantee"]["user"] == "reader"


def test_structured_grantee_and_member_routes_use_typed_fields() -> None:
    class _Api:
        def __init__(self) -> None:
            self.grants = []
            self.members = []

        def grant(self, grant, *, security):
            self.grants.append((grant, security.auth.actor))
            return grant.__class__(
                grant_id="generated-id",
                grantor=grant.grantor,
                grantee=grant.grantee,
                actions=grant.actions,
            )

        def add_space_member(self, org, space, member, *, security) -> None:
            self.members.append((org, space, member, security.auth.actor))

    api = _Api()
    srv = SimpleNamespace(api=api)
    actor = handler.Scope(org="acme", user="administrator")

    status, body = handler.dispatch(
        srv,
        DispatchRequest(
            verb="grant",
            actor=actor,
            target=handler.Scope(org="acme", user="owner"),
            security=internal_context(ScopedAuthenticator(actor)),
            grantee=handler.Scope(org="acme", user="reader"),
        ),
    )
    assert status == 200, body
    assert api.grants[0][0].grantor == handler.Scope(org="acme", user="owner")
    assert api.grants[0][0].grantee == handler.Scope(org="acme", user="reader")
    assert api.grants[0][1] == actor

    status, body = handler.dispatch(
        srv,
        DispatchRequest(
            verb="add_space_member",
            actor=actor,
            target=handler.Scope(org="acme", space="product"),
            security=internal_context(ScopedAuthenticator(actor)),
            member=handler.Scope(org="acme", user="reader"),
            payload={"role": "viewer"},
        ),
    )
    assert status == 200, body
    org, space, member, received_actor = api.members[0]
    assert (org, space) == ("acme", "product")
    assert member.scope == handler.Scope(org="acme", user="reader")
    assert member.role == "viewer"
    assert received_actor == actor


@pytest.mark.parametrize(
    "actor",
    [
        Scope(),  # 空 actor：身份未填内容，不是特权形态
        Scope(org="acme", user="outsider"),  # 非 owner：撤销别人的授权
    ],
)
def test_dispatch_revoke_rejects_non_owner_actors(actor: Scope) -> None:
    """撤销只允许 owner：身份由 security 携带，payload 无法再覆写 actor。"""
    srv = Server.build(load_config([OFFLINE]))

    if not actor.user:
        with pytest.raises(AuthenticationError):
            _security(actor)
        return
    status, granted = srv.dispatch(
        "grant",
        {"tenant_id": "acme", "scope": "owner", "grantee": "reader"},
        security=_security(Scope(org="acme", user="owner")),
    )
    assert status == 200, granted
    status, body = srv.dispatch(
        "revoke",
        {
            "tenant_id": "acme",
            "scope": "owner",
            "grantee": "reader",
            "grant_id": granted["grant_id"],
        },
        security=_security(actor),
    )

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_audit_forwards_structured_filters() -> None:
    class _Api:
        def __init__(self) -> None:
            self.filters = None

        def audit(self, filters, *, security, limit=100):
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

    status, body = _dispatch(
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
        def audit(filters, *, security, limit=100):
            raise AssertionError("audit should not be called with an invalid limit")

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    status, body = _dispatch(_Srv(), "audit", {"limit": limit})

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
            security,
            offset=0,
            limit=100,
            memory_types=None,
            extensions=None,
            filters=None,
        ):
            self.call = {
                "scope": scope,
                "identity": security.auth.actor,
                "offset": offset,
                "limit": limit,
                "memory_types": memory_types,
                "extensions": extensions,
                "filters": filters,
            }
            return MemoryListResult(
                items=[
                    handler.MemoryUnit(
                        id="unit-1",
                        scope=scope,
                        segments=[Segment(content="repo uses pytest")],
                        system_metadata={"memory_type": "coding"},
                    )
                ],
                count=7,
            )

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()
    status, body = _dispatch(
        srv,
        "list",
        {
            "tenant_id": "acme",
            "scope": "owner",
            # 历史 actor 键仍会被剥离出业务 payload，但**不再**构成身份：
            # actor 一律来自 security（本用例使用固定 local/developer 身份）。
            "actor_scope": "reader",
            "offset": "2",
            "limit": "5",
            "memory_types": "coding,episodic",
            "extensions": {"vendor_mode": 3},
            "filter": {"user_metadata.project": "alpha"},
        },
    )

    assert status == 200
    assert srv.api.call == {
        "scope": handler.Scope(org="acme", user="owner"),
        "identity": handler.Scope(org="local", user="developer"),
        "offset": 2,
        "limit": 5,
        "memory_types": ["coding", "episodic"],
        "extensions": {"vendor_mode": "3"},
        "filters": {"user_metadata.project": "alpha"},
    }
    assert body["ok"] is True
    assert body["op"] == "list"
    assert body["count"] == 7
    assert body["offset"] == 2
    assert body["limit"] == 5
    assert body["items"][0]["item_id"] == "unit-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"offset": -1},
        {"limit": 0},
        {"memory_types": {}},
        {"extensions": []},
        {"filters": {}, "filter": {}},
    ],
)
def test_dispatch_list_rejects_invalid_options(payload) -> None:
    class _Api:
        @staticmethod
        def list(*args, **kwargs):
            raise AssertionError("list should not be called with invalid options")

    class _Srv:
        api = _Api()

    status, body = _dispatch(
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

        def create_space(self, spec, *, security):
            self.call = {"spec": spec, "identity": security.auth.actor}
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
    status, body = _dispatch(
        srv,
        "create_space",
        {
            "tenant_id": "acme",
            "space": "coding",
            # 历史 actor 键被剥离、不构成身份：actor 来自固定的测试 security。
            "actor_space": "",
            "actor_scope": "",
            "display_name": "Coding",
            "principal_path": "agent_user",
            "policy": {"pipeline_profiles": {"coding": "coding"}},
            "metadata": {"env": "prod"},
        },
    )

    assert status == 200
    assert srv.api.call["identity"] == handler.Scope(org="local", user="developer")
    assert srv.api.call["spec"].org == "acme"
    assert srv.api.call["spec"].space == "coding"
    assert srv.api.call["spec"].principal_path == PrincipalPath.AGENT_USER
    assert srv.api.call["spec"].policy.pipeline_profiles == {"coding": "coding"}
    assert body["space"]["metadata"] == {"env": "prod"}
