"""管理面 verb 的 dispatch 兼容性。

原先本文件不带任何认证上下文直接 ``srv.dispatch(...)``，靠 ``_actor_scope``
从 payload 里凑出身份。身份改由认证上下文提供后（security.md §9 铁律 #1），
每条用例都必须显式声明「谁在发这个请求」——这正是要的效果：
**签名上就不给「不指定身份也能调」留位置**。
"""

from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager

import pytest

from common.type_def import Segment
from common.type_def.auth import AuthContext, Role, reset_current, set_current
from common.type_def.scope import Scope
from control import MemoryListResult, PrincipalPath, SpaceInfo, SpaceStatus

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

_OWNER = Scope(org="acme", user="owner")


@contextmanager
def _as(actor: Scope, role: Role = Role.USER):
    """以 ``actor`` 的身份发起请求（等价于中间件认证通过后的状态）。"""
    token = set_current(AuthContext(actor=actor, acting_user=actor.user, role=role))
    try:
        yield
    finally:
        reset_current(token)


def test_dispatch_admin_requires_platform_admin_under_default_kernel() -> None:
    srv = Server.build(load_config([OFFLINE]))
    with _as(Scope(org="acme", user="alice")):
        status, body = srv.dispatch("admin", {"tenant_id": "acme", "scope": "alice"})

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


def test_dispatch_admin_without_credentials_is_unauthenticated_not_forbidden() -> None:
    """无凭据是 401 而不是 403。

    401「不知道你是谁」与 403「知道你是谁但不许」是两件事；旧实现把前者伪装
    成后者（payload 凑出的身份恰好没权限），掩盖了「认证层根本不存在」。
    """
    srv = Server.build(load_config([OFFLINE]))
    status, body = srv.dispatch("admin", {})

    assert status == 401
    assert body["error"] == "AuthenticationError"


def test_dispatch_revoke_supports_scope_owner() -> None:
    srv = Server.build(load_config([OFFLINE]))

    with _as(_OWNER):
        status, body = srv.dispatch(
            "revoke",
            {"tenant_id": "acme", "scope": "owner", "grantee": "reader"},
        )

    assert status == 200
    assert body["grantee"]["user"] == "reader"


def test_dispatch_revoke_denied_for_non_owner() -> None:
    """撤销别人 scope 下的授权 → 403。

    旧用例靠 payload 里塞 ``actor_scope: outsider`` 制造这个非属主身份；
    现在身份来自上下文，构造方式变了，**要断言的行为没变**。
    """
    srv = Server.build(load_config([OFFLINE]))

    with _as(Scope(org="acme", user="outsider")):
        status, body = srv.dispatch(
            "revoke",
            {"tenant_id": "acme", "scope": "owner", "grantee": "reader"},
        )

    assert status == 403
    assert body["error"] == "PermissionDeniedError"


@pytest.mark.parametrize(
    "actor_override",
    [
        {"actor_tenant_id": ""},
        {"actor_tenant_id": "acme", "actor_scope": "outsider"},
    ],
)
def test_dispatch_revoke_rejects_payload_identity_claims(
    actor_override: dict[str, str],
) -> None:
    """payload 里的身份声明一律 400——包括曾经能命中全局放行的空 ``actor_tenant_id``。"""
    srv = Server.build(load_config([OFFLINE]))

    with _as(_OWNER):
        status, body = srv.dispatch(
            "revoke",
            {
                "tenant_id": "acme",
                "scope": "owner",
                "grantee": "reader",
                **actor_override,
            },
        )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert "identity must come from credentials" in body["message"]


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

    with _as(_OWNER):
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


def test_dispatch_audit_keeps_actor_agent_as_query_filter() -> None:
    """``audit`` 的 ``actor_agent`` / ``actor_session`` 是**查询谓词**不是身份声明。

    与身份声明字段同名但语义不同（筛「历史事件的操作者是谁」），故对该 verb 放行。
    """

    class _Api:
        def __init__(self) -> None:
            self.filters = None

        def audit(self, filters, *, identity, limit=100):
            self.filters = filters
            return []

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()

    with _as(_OWNER):
        status, _ = handler.dispatch(srv, "audit", {"actor_agent": "bot", "actor_session": "s1"})

    assert status == 200
    assert srv.api.filters == {"actor_agent": "bot", "actor_session": "s1"}


@pytest.mark.parametrize("limit", ["not-a-number", "", [], -1, 0])
def test_dispatch_audit_rejects_invalid_limit(limit) -> None:
    class _Api:
        @staticmethod
        def audit(filters, *, identity, limit=100):
            raise AssertionError("audit should not be called with an invalid limit")

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    with _as(_OWNER):
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
            extensions=None,
            filters=None,
        ):
            self.call = {
                "scope": scope,
                "identity": identity,
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
                        metadata={"memory_type": "coding"},
                    )
                ],
                count=7,
            )

    class _Srv:
        def __init__(self) -> None:
            self.api = _Api()

    srv = _Srv()
    # identity 从认证上下文来，不从 payload 的 actor_scope 来——后者现在会被拒。
    with _as(handler.Scope(org="acme", user="reader")):
        status, body = handler.dispatch(
            srv,
            "list",
            {
                "tenant_id": "acme",
                "scope": "owner",
                "offset": "2",
                "limit": "5",
                "memory_types": "coding,episodic",
                "extensions": {"vendor_mode": "3"},
                "filter": {"metadata.project": "alpha"},
            },
        )

    assert status == 200
    assert srv.api.call == {
        "scope": handler.Scope(org="acme", user="owner"),
        "identity": handler.Scope(org="acme", user="reader"),
        "offset": 2,
        "limit": 5,
        "memory_types": ["coding", "episodic"],
        "extensions": {"vendor_mode": "3"},
        "filters": {"metadata.project": "alpha"},
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

    with _as(handler.Scope(org="acme", user="reader")):
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
    # 上游原版在 payload 里塞 ``actor_space: ""`` / ``actor_scope: ""`` 来把 identity
    # 压成 ``Scope(org="acme")``；这两个字段现在会被拒。要断言的东西没变，
    # 换成从认证上下文给同一个身份。
    with _as(handler.Scope(org="acme")):
        status, body = handler.dispatch(
            srv,
            "create_space",
            {
                "tenant_id": "acme",
                "space": "coding",
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
