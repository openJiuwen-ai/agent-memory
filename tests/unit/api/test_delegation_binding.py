# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""私有凭据绑定的真实装配及 PEP 回归：actor 不变、作者派生、撤销即时生效。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import (
    Credentials,
    Scope,
    Surface,
    assemble_runtime,
    new_request_context,
)
from jiuwen_memory.api.memory_api_impl.local_support import _TrustedRequestTarget
from jiuwen_memory.common.errors import AuthenticationError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security._delegation_binding import _stores
from jiuwen_memory.common.security.authentication.key_store import fingerprint
from jiuwen_memory.common.security.types import Action, Role
from jiuwen_memory.common.type_def import Context
from jiuwen_memory.control.types import SpaceMember, SpaceSpec
from tests.integration.jiuwen_memory_entry.fixtures import collective_settings

pytestmark = pytest.mark.unit


@pytest.fixture(params=["memory", "sqlite"])
def delegated_runtime(request):
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    settings = collective_settings()["memory_api"]
    settings["delegation_store"]["default"] = request.param
    settings["grant_store"]["default"] = request.param
    runtime = assemble_runtime(config=settings)
    auth = runtime._security_runtime.authenticator

    def context(token):
        return new_request_context(
            auth.authenticate(Credentials(api_key=token)), surface=Surface.SDK
        )

    api = runtime.api
    for space, user in (("u-u1", "u1"), ("u-u2", "u2"), ("team-t", "u3")):
        api.create_space(
            SpaceSpec(org="local", space=space, owner=Scope(org="local", user=user)),
            security=context("test-ops"),
        )
    api.add_space_member(
        "local", "team-t", SpaceMember(scope=Scope(user="u1")), security=context("test-u3")
    )
    try:
        yield runtime, context, _stores(runtime.api._authorizer)[0]
    finally:
        runtime.close()


def test_delegated_write_read_list_search_keep_real_actor(delegated_runtime, monkeypatch):
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    runtime, context, _store = delegated_runtime
    security = context("test-u1-agent")
    assert security.auth.actor == Scope(org="local", agent="a1", session="s1")
    assert security.auth.delegation_id == "u1-a1"
    assert security.auth.credential_id == fingerprint("test-u1-agent")
    api = runtime.api
    events = []
    monkeypatch.setattr(api, "_record_audit", lambda actor, *args, **kw: events.append(actor))

    def unrelated_grants(**kwargs):
        pytest.fail("valid owner/member delegation must not consult unrelated GrantStore")

    for grant_store in api._authorizer.management_grant_stores():
        monkeypatch.setattr(grant_store, "find_active", unrelated_grants)
    target = Scope(org="local", space="u-u1")
    unit = api.add("用户习惯用 Python 写代码", target, security=security)[0]
    assert unit.system_metadata["author_principal"] == "user:u1"
    assert unit.system_metadata["author_agent"] == "a1"
    assert api.get(unit.id, target, security=security).id == unit.id
    assert [item.id for item in api.list(target, security=security).items] == [unit.id]
    for extensions in ({}, {"spaces": []}):
        found = api.search(
            "Python", Context(scope=target, extensions=extensions), security=security
        )
        assert unit.id in {item.unit_id for item in found.items}
    monkeypatch.setattr(api, "_space_fanout_limit", lambda: 1)
    api.search(
        "Python",
        Context(scope=target, extensions={"spaces": ["u-u1", "team-t"]}),
        security=security,
    )
    assert events and all(actor == security.auth.actor for actor in events)


@pytest.mark.parametrize("change", ["revoked", "expired", "future", "credential", "session"])
def test_binding_rechecked_for_cached_context_and_new_authentication(delegated_runtime, change):
    runtime, context, store = delegated_runtime
    security = context("test-u1-agent")
    record = store.get("u1-a1")
    now = datetime.now(timezone.utc)
    updates = {
        "expired": {"expires_at": now - timedelta(seconds=1)},
        "future": {"not_before": now + timedelta(hours=1)},
        "credential": {"bound_credential_id": fingerprint("different")},
        "session": {"bound_session": "another-session"},
    }
    if change == "revoked":
        store.revoke("u1-a1")
    else:
        update = updates.get(change)
        assert update is not None, f"unknown binding change: {change}"
        store.add(replace(record, **update))
    with pytest.raises(AuthenticationError, match="authentication failed"):
        context("test-u1-agent")
    with pytest.raises(PermissionDeniedError):
        runtime.api.add("denied", Scope(org="local", space="u-u1"), security=security)


def test_delegation_respects_action_space_membership_and_governance(delegated_runtime):
    runtime, context, store = delegated_runtime
    api = runtime.api
    security = context("test-u1-agent")
    target = Scope(org="local", space="team-t")
    assert api.add("team", target, security=security)
    with pytest.raises(PermissionDeniedError):
        api.add("foreign", Scope(org="local", space="u-u2"), security=security)
    with pytest.raises(PermissionDeniedError):
        api.get_space_policy("local", "team-t", security=security)
    api.remove_space_member("local", "team-t", Scope(user="u1"), security=context("test-u3"))
    with pytest.raises(PermissionDeniedError):
        api.add("lost membership", target, security=security)
    store.add(replace(store.get("u1-a1"), actions=frozenset()))
    with pytest.raises(PermissionDeniedError):
        api.add("no action", Scope(org="local", space="u-u1"), security=security)


def test_unbound_and_forged_input_cannot_create_delegated_author(delegated_runtime):
    runtime, context, _store = delegated_runtime
    api = runtime.api
    for token in ("test-unbound-agent", "test-u1-agent"):
        with pytest.raises(ValidationError):
            api.add(
                "spoof",
                Scope(org="local", user="u2"),
                security=context(token),
                system_metadata={"coords": {}},
            )
    with pytest.raises(PermissionDeniedError):
        api.add(
            "spoof",
            Scope(org="local", space="u-u1"),
            security=context("test-unbound-agent"),
            system_metadata={"_delegator.user": "u1", "_delegator.owner_covered": "true"},
        )
    with pytest.raises(ValidationError):
        api.add(
            "spoof",
            Scope(org="local", space="u-u1"),
            security=context("test-u1-agent"),
            system_metadata={"author_principal": "user:u2"},
        )


def test_binding_rejects_a_different_authorizer_store():
    config = collective_settings()["memory_api"]
    config["delegation_store"]["other"] = "memory"
    config["security"]["default"]["params"]["delegation_store"] = "other"
    with pytest.raises(ValidationError, match="same DelegationStore"):
        assemble_runtime(config=config)


@pytest.mark.parametrize(
    "spaces,allowed",
    [([","], False), (["space,other"], False), (["u-u1", "team-t"], True), ([], True)],
)
def test_bound_delegation_spaces_never_expand(delegated_runtime, spaces, allowed):
    runtime, context, store = delegated_runtime
    record = store.get("u1-a1")
    store.add(replace(record, allowed_spaces=frozenset(spaces)))
    security = context("test-u1-agent")
    target = Scope(org="local", space="u-u1")
    if allowed:
        assert runtime.api.add("allowed", target, security=security)
    else:
        with pytest.raises(PermissionDeniedError):
            runtime.api.add("denied", target, security=security)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actions", ["manage_space"]),
        ("expires_at", "2027-01-01T00:00:00"),
        ("delegate", {"org": "local", "user": "u1", "agent": "a1"}),
        ("delegator", {"org": "local", "user": "u1", "session": "s1"}),
        ("delegation_id", ""),
        ("delegation_id", 1),
        ("delegator", {"user": "u1"}),
        ("delegator", {"org": "local"}),
        ("delegator", {"org": "local", "user": "u1", "agent": "a1"}),
        ("delegate", {"org": "local"}),
        ("delegate", {"org": "other", "agent": "a1"}),
        ("revoked", "false"),
        ("bound_credential_id", 1),
        ("bound_session", None),
    ],
)
def test_invalid_seed_configuration_fails_closed(field, value):
    config = collective_settings()["memory_api"]
    config["security"]["default"]["params"]["delegations"][0][field] = value
    with pytest.raises(ValidationError, match="delegation configuration"):
        assemble_runtime(config=config)


@pytest.mark.parametrize(
    "bindings",
    [[], {}, {"": "u1-a1"}, {1: "u1-a1"}, {"fingerprint": ""}, {"fingerprint": 1}],
)
def test_invalid_binding_map_fails_closed(bindings):
    config = collective_settings()["memory_api"]
    config["security"]["default"]["params"]["delegation_bindings"] = bindings
    with pytest.raises(ValidationError, match="delegation_bindings"):
        assemble_runtime(config=config)


def test_bound_api_key_still_uses_real_credential_revocation():
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    config = collective_settings()["memory_api"]
    config["security"]["default"]["params"]["authenticator"] = {"target": "api_key"}
    runtime = assemble_runtime(config=config)
    try:
        bound = runtime._security_runtime.authenticator
        # 模拟部署时已经签发并写入服务端配置的凭据，避免固定真实密钥。
        key_store = bound._delegate.key_store
        token = key_store.issue(Scope(org="local", agent="a1", session="s1"), Role.USER)
        credential_id = fingerprint(token)
        bound._bindings[credential_id] = "u1-a1"
        store = _stores(runtime.api._authorizer)[0]
        store.add(replace(store.get("u1-a1"), bound_credential_id=credential_id))
        security = new_request_context(
            bound.authenticate(Credentials(api_key=token)), surface=Surface.SDK
        )
        registry_key = (security.auth.credential_type, security.auth.credential_issuer)
        assert runtime.api._credentials._stores[registry_key] is key_store
        runtime.api._require_trusted_context(
            security, _TrustedRequestTarget(Action.WRITE, "add", Scope(org="local"))
        )
        key_store.revoke(credential_id)
        with pytest.raises(PermissionDeniedError):
            runtime.api._require_trusted_context(
                security, _TrustedRequestTarget(Action.WRITE, "add", Scope(org="local"))
            )
        with pytest.raises(AuthenticationError):
            bound.authenticate(Credentials(api_key=token))
    finally:
        runtime.close()
