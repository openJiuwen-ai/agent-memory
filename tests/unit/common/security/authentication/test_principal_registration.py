# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""无 Key 主体登记与原 API Key 生命周期的兼容、冲突和原子性。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.authentication.key_store import (
    KeyStoreProducer,
    PrincipalRegistrationCapability,
    fingerprint,
)
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    register_plugins()
    return KeyStoreProducer.build("memory", {}, AssemblyContext())


def test_registration_does_not_issue_api_key(store, monkeypatch):
    actor = Scope(org="demo", user="alice")

    def unexpected_issue(*_args, **_kwargs):
        raise AssertionError("Principal registration must not issue an API Key")

    monkeypatch.setattr(store, "issue", unexpected_issue)
    assert isinstance(store, PrincipalRegistrationCapability)
    assert store.register_principal(actor, Role.USER) is None
    assert store.get_role(actor) is Role.USER
    store.register_principal(actor, Role.USER)
    actor.user = "changed"
    assert store.get_role(Scope(org="demo", user="alice")) is Role.USER
    assert store.get_role(actor) is None


@pytest.mark.parametrize(
    "actor",
    [
        Scope(),
        Scope(org="demo"),
        Scope(org="demo", user="alice", agent="agent1"),
        Scope(org="demo", user="alice", space="team"),
        Scope(org="demo", user="alice", session="session1"),
        Scope(org=" ", user="alice"),
        Scope(org="demo", user=" alice "),
        Scope(org=123, user="alice"),
        None,
    ],
)
def test_registration_rejects_invalid_identity_without_state_change(store, actor):
    with pytest.raises(ValidationError):
        store.register_principal(actor, Role.USER)
    assert store.get_role(Scope(org="demo", user="alice")) is None


def test_registration_rejects_root_and_non_enum_roles(store):
    actor = Scope(org="demo", user="alice")
    with pytest.raises(PermissionDeniedError):
        store.register_principal(actor, Role.ROOT)
    with pytest.raises(ValidationError):
        store.register_principal(actor, "root")
    assert store.get_role(actor) is None


def test_key_revocation_does_not_delete_independently_registered_principal(store):
    actor = Scope(org="demo", agent="assistant")
    store.register_principal(actor, Role.USER)
    key = store.issue(actor, Role.USER)
    store.revoke(fingerprint(key))
    assert store.resolve(key) is None
    assert store.get_role(actor) is Role.USER


def test_key_only_principal_keeps_legacy_revocation_semantics(store):
    actor = Scope(org="demo", user="key-only")
    key = store.issue(actor, Role.USER)
    store.revoke(fingerprint(key))
    assert store.get_role(actor) is None


@pytest.mark.parametrize("first", ["register", "issue"])
def test_conflicting_roles_cannot_overwrite_registration_or_active_key(store, first):
    actor = Scope(org="demo", user="alice")
    if first == "register":
        store.register_principal(actor, Role.ADMIN)
        with pytest.raises(ValidationError):
            store.issue(actor, Role.USER)
    else:
        key = store.issue(actor, Role.ADMIN)
        with pytest.raises(ValidationError):
            store.register_principal(actor, Role.USER)
        store.revoke(fingerprint(key))
        assert store.get_role(actor) is None
        return
    assert store.get_role(actor) is Role.ADMIN


def test_register_after_key_issuance_survives_last_key_revocation(store):
    actor = Scope(org="demo", user="alice")
    key = store.issue(actor, Role.USER)
    store.register_principal(actor, Role.USER)
    store.revoke(fingerprint(key))
    assert store.get_role(actor) is Role.USER
    with pytest.raises(ValidationError):
        store.register_principal(actor, Role.ADMIN)


def test_concurrent_key_issuance_and_registration_have_one_role(store):
    actor = Scope(org="demo", user="alice")
    barrier = Barrier(2)

    def register():
        barrier.wait(timeout=5)
        try:
            store.register_principal(actor, Role.ADMIN)
            return "registered", None
        except ValidationError:
            return "conflict", None

    def issue():
        barrier.wait(timeout=5)
        try:
            return "issued", store.issue(actor, Role.USER)
        except ValidationError:
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register), pool.submit(issue)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(state == "conflict" for state, _ in results) == 1
    issued = next((key for state, key in results if state == "issued"), None)
    if issued is not None:
        assert store.resolve(issued).role is Role.USER
        assert store.get_role(actor) is Role.USER
    else:
        assert store.get_role(actor) is Role.ADMIN


def test_explicit_registration_survives_revoke_without_misleading_role_change_hint(store):
    actor = Scope(org="demo", user="initialized")
    store.register_principal(actor, Role.USER)
    key = store.issue(actor, Role.USER)
    store.revoke(fingerprint(key))
    with pytest.raises(ValidationError, match="initialized principal"):
        store.issue(actor, Role.ADMIN)
    assert store.get_role(actor) is Role.USER
