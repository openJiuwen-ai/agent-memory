"""独立验收 R1/R2/R5–R8 的真实装配回归。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import AuthenticationError, BackendError, PermissionDeniedError
from jiuwen_memory.common.security import internal_context, new_request_context
from jiuwen_memory.common.security.space_roles import SpaceGovernanceRole
from jiuwen_memory.common.security.types import (
    Action,
    AuthContext,
    Delegation,
    Grant,
    RequestSecurityContext,
    Role,
    Surface,
)
from jiuwen_memory.common.type_def import Context, Scope
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.types import DeleteSelector, SpaceMember, SpaceSpec
from tests.integration.jiuwen_memory_entry.fixtures import collective_settings
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("org", ["victim", "attacker"])
def test_revoke_checks_real_grantor(backend, org):
    # 从实际 PDP 真源确认拒绝撤权没有误删记录。
    # pylint: disable=protected-access
    runtime = assemble_runtime(config={"grant_store": {"default": backend}})
    try:
        api = runtime.api
        owner = Scope(org="victim", user="alice")
        attacker = Scope(org=org, user="mallory")
        security = internal_context(ScopedAuthenticator(owner))
        grant = api.grant(
            Grant(
                grantor=owner,
                grantee=Scope(org="victim", user="bob"),
                actions=frozenset({Action.READ}),
            ),
            security=security,
        )
        with pytest.raises(PermissionDeniedError):
            api.revoke(
                replace(grant, grantor=attacker),
                security=internal_context(ScopedAuthenticator(attacker)),
            )
        store = api._authorizer.management_grant_store()
        assert store.find_active(
            grantee=grant.grantee,
            grantor_org="victim",
            action=Action.READ,
            now=datetime.now(timezone.utc),
        )
        api.revoke(grant, security=security)
        api.revoke(grant, security=security)
        api.revoke(replace(grant, grant_id="unknown"), security=security)
        assert not store.find_active(
            grantee=grant.grantee,
            grantor_org="victim",
            action=Action.READ,
            now=datetime.now(timezone.utc),
        )
    finally:
        runtime.close()


@pytest.fixture
def collective_api():
    runtime = assemble_runtime(config=collective_settings()["memory_api"])
    root = internal_context(ScopedAuthenticator(Scope(org="system", user="root"), role=Role.ROOT))
    try:
        yield runtime.api, root
    finally:
        runtime.close()


@pytest.mark.parametrize("method", ["get", "inspect", "trace"])
def test_every_unit_read_rejects_another_author(collective_api, method):
    api, root = collective_api
    agent = Scope(org="local", agent="a1")
    scope = Scope(org="local", space="agent-space")
    api.create_space(SpaceSpec(org=scope.org, space=scope.space, owner=agent), security=root)
    unit = api.add("secret-by-another-author", scope, security=root)[0]
    security = internal_context(ScopedAuthenticator(agent))
    assert not api.list(scope, security=security).items
    with pytest.raises(PermissionDeniedError):
        getattr(api, method)(
            [unit.id] if method == "inspect" else unit.id, scope, security=security
        )


def test_trace_checks_each_ancestor_author(collective_api):
    # 白盒写入跨作者血缘，验证 trace 对祖先逐条判权。
    # pylint: disable=protected-access
    api, root = collective_api
    agent = Scope(org="local", agent="a1")
    target = Scope(org="local", space="agent-space")
    api.create_space(SpaceSpec(org=target.org, space=target.space, owner=agent), security=root)
    denied = api.add("private ancestor", target, security=root)[0]
    security = internal_context(ScopedAuthenticator(agent))
    child = api.add("visible child", target, security=security)[0]
    child.provenance = [denied.id]
    api._governance._governor._kv.update(child.scope, memory_key(child.id), dumps(child))
    assert api.get(child.id, target, security=security).id == child.id
    with pytest.raises(PermissionDeniedError):
        api.trace(child.id, target, security=security)


@pytest.mark.parametrize("route_key", ["memory_type", "pipeline"])
@pytest.mark.parametrize("entry", ["get", "inspect", "trace"])
def test_governance_uses_actual_unit_type_route(route_key, entry):
    # 白盒装配不同路由的委托真源并构造血缘，不扩充冻结接口。
    # pylint: disable=protected-access
    # 两个真实 StandardAuthorizer 共享 Grant 真源，但只有 fallback 的委托源授予 READ。
    config = {
        "authorizer": {
            "default": {
                "target": "routing",
                "params": {
                    "route_key": route_key,
                    "routes": {"private": "strict"},
                    "fallback": "normal",
                },
            },
            "normal": {
                "target": "standard",
                "params": {"grant_store": "default", "delegation_store": "normal"},
            },
            "strict": {
                "target": "standard",
                "params": {"grant_store": "default", "delegation_store": "strict"},
            },
        },
        "delegation_store": {"default": "memory", "normal": "memory", "strict": "memory"},
        "grant_store": {"default": "memory"},
    }
    runtime = assemble_runtime(config=config)
    try:
        api = runtime.api
        owner = Scope(org="local", user="alice")
        actor = Scope(org="local", agent="bot")
        owner_security = internal_context(ScopedAuthenticator(owner))
        normal = api._authorizer._policies["normal"]
        normal._delegations.add(
            Delegation(
                delegation_id="read-public",
                delegator=owner,
                delegate=actor,
                actions=frozenset({Action.READ}),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        security = internal_context(ScopedAuthenticator(actor, delegation_id="read-public"))
        public = api.add("public", owner, security=owner_security)[0]
        private = api.add(
            "private", owner, security=owner_security, system_metadata={route_key: "private"}
        )[0]
        read = getattr(api, entry)
        assert read([public.id] if entry == "inspect" else public.id, owner, security=security)
        with pytest.raises(PermissionDeniedError):
            read([private.id] if entry == "inspect" else private.id, owner, security=security)
        if entry == "trace":
            public.provenance = [private.id]
            api._governor._kv.update(owner, memory_key(public.id), dumps(public))
            with pytest.raises(PermissionDeniedError):
                api.trace(public.id, owner, security=security)
    finally:
        runtime.close()


def test_root_can_grant_and_manage_members(collective_api):
    api, root = collective_api
    scope = Scope(org="local", space="u-u1")
    api.create_space(
        SpaceSpec(org=scope.org, space=scope.space, owner=Scope(org="local", user="u1")),
        security=root,
    )
    member = SpaceMember(scope=Scope(user="u2"), governance_role=SpaceGovernanceRole.MANAGER)
    api.add_space_member(scope.org, scope.space, member, security=root)
    api.remove_space_member(scope.org, scope.space, member.scope, security=root)
    grant = api.grant(
        Grant(
            grantor=scope, grantee=Scope(org="local", user="u2"), actions=frozenset({Action.READ})
        ),
        security=root,
    )
    api.revoke(grant, security=root)


@pytest.mark.parametrize("method", ["delete", "job_status", "job_cancel"])
def test_untrusted_context_rejected_before_backend(collective_api, monkeypatch, method):
    # 在业务后端放置失败探针，确认不可信上下文在读取前被拒绝。
    # pylint: disable=protected-access
    api, _ = collective_api
    security = RequestSecurityContext(auth=AuthContext(actor=Scope(org="local", user="u1")))

    def backend(*args, **kwargs):
        pytest.fail("untrusted request reached backend")

    if method == "delete":
        monkeypatch.setattr(api._queries, "permission_contexts_for_delete", backend)
        args = [DeleteSelector(unit_ids=["unknown"])]
    else:
        monkeypatch.setattr(api._scheduler, "status", backend)
        args = ["unknown"]
    with pytest.raises(PermissionDeniedError):
        getattr(api, method)(*args, security=security)


@pytest.mark.parametrize(
    "actor", [Scope(user="u1"), Scope(org="local"), Scope(org="local", user="u1", agent="a1")]
)
@pytest.mark.parametrize("internal", [False, True])
def test_context_constructors_reject_invalid_actor(actor, internal):
    with pytest.raises(AuthenticationError):
        if internal:
            internal_context(ScopedAuthenticator(actor))
        else:
            new_request_context(AuthContext(actor=actor), surface=Surface.SDK)


@pytest.mark.parametrize("routed", [False, True])
def test_routing_preserves_backend_errors(collective_api, monkeypatch, routed):
    # 在实际 PDP 的 Store 注入故障，不能用替身绕过真实授权链。
    # pylint: disable=protected-access
    api, root = collective_api
    actor = Scope(org="local", user="u1")
    scope = Scope(org="local", space="u-u1")
    api.create_space(SpaceSpec(org=scope.org, space=scope.space, owner=actor), security=root)

    def unavailable(**kwargs):
        raise BackendError("unavailable")

    for store in api._authorizer.management_grant_stores():
        monkeypatch.setattr(store, "find_active", unavailable)
    with pytest.raises(BackendError):
        api.add(
            "content",
            scope,
            security=internal_context(ScopedAuthenticator(actor)),
            system_metadata={"coords": {}} if routed else None,
        )


def test_cross_space_search_does_not_misreport_backend_as_denial(collective_api, monkeypatch):
    # 在实际 PDP 的 Store 注入故障，核实后端不可用与权限拒绝的区别。
    # pylint: disable=protected-access
    api, root = collective_api
    target = Scope(org="local", space="u-u1")
    actor = Scope(org="local", user="u1")
    api.create_space(SpaceSpec(org=target.org, space=target.space, owner=actor), security=root)

    def unavailable(**kwargs):
        raise BackendError("unavailable")

    for store in api._authorizer.management_grant_stores():
        monkeypatch.setattr(store, "find_active", unavailable)
    with pytest.raises(BackendError):
        api.search(
            "query",
            Context(scope=target, extensions={"spaces": [target.space]}),
            security=internal_context(ScopedAuthenticator(actor)),
        )


@pytest.mark.parametrize("failure", [BackendError, PermissionDeniedError])
def test_cross_space_partial_result_keeps_error_category(collective_api, monkeypatch, failure):
    # 仅对一个空间注入 PDP 故障，其他空间继续使用真实判定。
    # pylint: disable=protected-access
    api, root = collective_api
    actor = Scope(org="local", user="u1")
    security = internal_context(ScopedAuthenticator(actor))
    for space in ("u-u1", "unavailable"):
        api.create_space(SpaceSpec(org="local", space=space, owner=actor), security=root)
    good = Scope(org="local", space="u-u1")
    unit = api.add("Python coding preference", good, security=security)[0]
    authorize = api._authorizer.authorize

    def fault(**kwargs):
        if kwargs["resource"].scope.space == "unavailable":
            raise failure("injected failure")
        return authorize(**kwargs)

    monkeypatch.setattr(api._authorizer, "authorize", fault)
    result = api.search(
        "Python",
        Context(scope=good, extensions={"spaces": ["u-u1", "unavailable"]}),
        security=security,
    )
    assert unit.id in {hit.unit_id for hit in result.items}
    assert any(error.error_type == failure.__name__ for error in result.errors)


def test_org_admin_does_not_inherit_root_content_grant_ceiling(collective_api):
    api, root = collective_api
    target = Scope(org="local", space="u-u1")
    api.create_space(
        SpaceSpec(org="local", space="u-u1", owner=Scope(org="local", user="u1")), security=root
    )
    admin = internal_context(ScopedAuthenticator(Scope(org="local", user="ops"), role=Role.ADMIN))
    with pytest.raises(PermissionDeniedError):
        api.grant(
            Grant(
                grantor=target,
                grantee=Scope(org="local", user="u2"),
                actions=frozenset({Action.READ}),
            ),
            security=admin,
        )
