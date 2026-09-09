# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""开发认证器的固定身份与安全能力契约。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.authentication_impl import DevAuthenticator
from jiuwen_memory.common.security.types import Credentials, Role
from jiuwen_memory.common.type_def import Scope

pytestmark = pytest.mark.unit


def test_dev_authenticator_ignores_credentials_and_returns_named_root() -> None:
    authenticator = DevAuthenticator()

    missing = authenticator.authenticate(Credentials())
    supplied = authenticator.authenticate(Credentials(api_key="ignored"))

    assert missing == supplied
    assert missing.actor == Scope(org="local", user="developer")
    assert missing.role is Role.ROOT
    assert missing.credential_type == "dev"
    assert missing.auth_method == "dev"
    assert missing.actor != Scope()


def test_dev_authenticator_declares_local_lightweight_capabilities() -> None:
    authenticator = DevAuthenticator()

    assert authenticator.mode() == "dev"
    assert authenticator.requires_loopback_binding() is True
    assert authenticator.requires_concurrency_guard() is False
    assert authenticator.health() is None


def _identity_config():
    return {
        "test-ops": {"actor": {"org": "local"}, "role": "admin"},
        "test-u1": {"actor": {"org": "local", "user": "u1"}},
        "test-u2": {"actor": {"org": "local", "user": "u2"}},
    }


def test_dev_identity_map_selects_server_owned_actor_and_role() -> None:
    authenticator = DevAuthenticator(identities=_identity_config())

    ops = authenticator.authenticate(Credentials(api_key="test-ops"))
    user = authenticator.authenticate(
        Credentials(api_key="test-u1", headers={"x-user": "u2", "x-role": "root"})
    )

    assert ops.actor == Scope(org="local")
    assert ops.role is Role.ADMIN
    assert user.actor == Scope(org="local", user="u1")
    assert user.role is Role.USER
    assert user.auth_method == "dev"
    assert authenticator.requires_loopback_binding() is True
    assert authenticator.requires_concurrency_guard() is False


@pytest.mark.parametrize("key", ["", "unknown", "test-u1 "])
def test_dev_identity_map_never_falls_back_for_missing_or_unknown_key(key) -> None:
    authenticator = DevAuthenticator(identities=_identity_config())

    with pytest.raises(AuthenticationError, match="^authentication failed$"):
        authenticator.authenticate(Credentials(api_key=key))


def test_dev_identity_map_copies_configuration_and_each_returned_actor() -> None:
    identities = _identity_config()
    authenticator = DevAuthenticator(identities=identities)
    identities["test-u1"]["actor"]["user"] = "changed"
    identities.clear()

    first = authenticator.authenticate(Credentials(api_key="test-u1"))
    first.actor.user = "tampered"
    second = authenticator.authenticate(Credentials(api_key="test-u1"))
    other = authenticator.authenticate(Credentials(api_key="test-u2"))

    assert second.actor == Scope(org="local", user="u1")
    assert other.actor == Scope(org="local", user="u2")
    assert second.actor is not first.actor


@pytest.mark.parametrize(
    "identities",
    [
        {}, [], "test-u1", {"": {"actor": {"org": "local"}}},
        {"bad key": {"actor": {"org": "local"}}},
        {"token": None}, {"token": {}}, {"token": {"actor": None}},
        {"token": {"actor": {}}},
        {"token": {"actor": {"org": "local", "user": 1}}},
        {"token": {"actor": {"org": "local", "unknown": "x"}}},
        {"token": {"actor": {"org": "local"}, "unknown": "x"}},
        {"token": {"actor": {"org": "local"}, "role": "superuser"}},
    ],
)
def test_dev_identity_map_rejects_invalid_configuration(identities) -> None:
    with pytest.raises(ValidationError):
        DevAuthenticator(identities=identities)


def test_dev_identity_map_and_fixed_actor_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        DevAuthenticator(Scope(org="local", user="fixed"), identities=_identity_config())
