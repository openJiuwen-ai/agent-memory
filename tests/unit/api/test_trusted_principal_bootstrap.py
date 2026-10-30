# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""只用公共 API，从纯 Trusted 配置初始化主体；旧插件契约保持兼容。"""

import pytest

from jiuwen_memory.api import (
    AuthenticationError,
    Credentials,
    PermissionDeniedError,
    Scope,
    UnsupportedCapabilityError,
    ValidationError,
    assemble_server_runtime,
    build_dev_authenticator,
    register_trusted_principal,
)
from jiuwen_memory.common.security.authentication.authentication_impl.trusted_authenticator import (
    TrustedAuthenticator,
)
from jiuwen_memory.common.security.authentication.key_store import PrincipalKeyStore
from jiuwen_memory.common.security.types import Role

pytestmark = pytest.mark.unit
GATEWAY_KEY = "gateway-key-for-bootstrap-tests"


@pytest.fixture
def runtime():
    runtime = assemble_server_runtime(
        config={
            "security": {
                "default": {
                    "target": "standard",
                    "params": {
                        "authenticator": "gateway",
                        "authorizer": "default",
                    },
                }
            },
            "authenticator": {
                "gateway": {
                    "target": "trusted",
                    "params": {
                        "key_store": "principals",
                        "gateway_key": GATEWAY_KEY,
                    },
                }
            },
            "key_store": {"principals": {"target": "memory"}},
            "authorizer": {
                "default": {
                    "target": "standard",
                    "params": {
                        "grant_store": "default",
                        "delegation_store": "default",
                    },
                }
            },
            "grant_store": {"default": {"target": "memory"}},
            "delegation_store": {"default": {"target": "memory"}},
        }
    )
    try:
        yield runtime
    finally:
        runtime.close(wait=True)


def credentials(principal="alice", kind="user", key=GATEWAY_KEY):
    return Credentials(
        api_key=key,
        headers={
            "x-org-id": "demo",
            "x-principal-type": kind,
            "x-principal-id": principal,
            "x-role": "root",
        },
    )


@pytest.mark.parametrize(
    ("kind", "principal", "role"),
    [
        ("user", "alice", "user"),
        ("agent", "assistant", "admin"),
    ],
)
def test_public_bootstrap_registers_actual_runtime_without_key(runtime, kind, principal, role):
    actor = Scope(org="demo", **{kind: principal})
    auth = runtime.security_runtime.authenticator
    with pytest.raises(AuthenticationError):
        runtime.authenticate(credentials(principal, kind))
    register_trusted_principal(auth, actor, role=role)
    register_trusted_principal(auth, actor, role=role)
    context = runtime.authenticate(credentials(principal, kind))
    assert context.auth.actor == actor
    assert context.auth.role.value == role
    unit = runtime.api.add("trusted owner", actor, security=context)[0]
    assert runtime.api.get(unit.id, actor, security=context).content == "trusted owner"
    with pytest.raises(PermissionDeniedError):
        runtime.api.add("forbidden", Scope(org="demo", user="bob"), security=context)
    with pytest.raises(AuthenticationError):
        runtime.authenticate(credentials("unregistered"))
    with pytest.raises(AuthenticationError):
        runtime.authenticate(credentials(principal, kind, key="wrong-gateway-key"))


@pytest.mark.parametrize("role", ["root", "invalid", "", None, 123])
def test_public_bootstrap_rejects_roles_without_registering(runtime, role):
    with pytest.raises((PermissionDeniedError, ValidationError)):
        register_trusted_principal(
            runtime.security_runtime.authenticator, Scope(org="demo", user="alice"), role=role
        )
    with pytest.raises(AuthenticationError):
        runtime.authenticate(credentials())


def test_conflicting_bootstrap_does_not_change_role(runtime):
    auth = runtime.security_runtime.authenticator
    actor = Scope(org="demo", user="alice")
    register_trusted_principal(auth, actor)
    with pytest.raises(ValidationError):
        register_trusted_principal(auth, actor, role="admin")
    assert runtime.authenticate(credentials()).auth.role.value == "user"


def test_bootstrap_does_not_mutate_other_runtimes(runtime):
    other = assemble_server_runtime(
        config={
            "security": {"default": {"target": "standard", "params": {"authenticator": "gateway"}}},
            "authenticator": {
                "gateway": {
                    "target": "trusted",
                    "params": {
                        "key_store": "principals",
                        "gateway_key": GATEWAY_KEY,
                    },
                }
            },
            "key_store": {"principals": {"target": "memory"}},
        }
    )
    try:
        register_trusted_principal(
            runtime.security_runtime.authenticator, Scope(org="demo", user="alice")
        )
        with pytest.raises(AuthenticationError):
            other.authenticate(credentials())
    finally:
        other.close(wait=True)


class ExistingStore(PrincipalKeyStore):
    def issue(self, actor, role):
        raise NotImplementedError

    def resolve(self, api_key):
        return None

    def revoke(self, key_fp):
        pass

    def get_role(self, actor):
        return Role.USER if actor == Scope(org="demo", user="alice") else None

    def health(self):
        pass


def test_existing_store_without_registration_capability_remains_usable():
    auth = TrustedAuthenticator(ExistingStore(), gateway_key=GATEWAY_KEY)
    with pytest.raises(UnsupportedCapabilityError) as error:
        register_trusted_principal(auth, Scope(org="demo", user="bob"))
    assert error.value.capability == "principal_registration"
    assert auth.authenticate(credentials()).role is Role.USER


def test_non_trusted_authenticator_without_capability_is_rejected():
    with pytest.raises(UnsupportedCapabilityError):
        register_trusted_principal(build_dev_authenticator(), Scope(org="demo", user="alice"))


def test_non_callable_capability_does_not_leak_type_error():
    auth = build_dev_authenticator()
    auth.register_principal = 0
    with pytest.raises(UnsupportedCapabilityError):
        register_trusted_principal(auth, Scope(org="demo", user="alice"))
