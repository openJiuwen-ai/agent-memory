# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK 与 Server 同源装配，撤销前签发的上下文也须逐请求失效。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    Credentials,
    PermissionDeniedError,
    Scope,
    Surface,
    ValidationError,
    assemble_runtime,
    assemble_server_runtime,
    build_dev_authenticator,
    new_request_context,
)
from jiuwen_memory.common.errors import AuthenticationError
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.authentication.key_store import KeyStoreProducer, fingerprint
from jiuwen_memory.common.security.types import Role

pytestmark = pytest.mark.unit


class PublicPluginAuthenticator(Authenticator):
    """只通过公开认证、凭据真源和资源依赖契约协作的第三方替身。"""

    def __init__(self, store):
        self.store = store
        self.issuer = "public-plugin"

    def mode(self):
        return self.issuer

    def health(self):
        self.store.health()

    def authenticate(self, credentials):
        identity = self.store.resolve(credentials.api_key)
        if identity is None:
            raise AuthenticationError("authentication failed")
        return replace(
            identity,
            credential_type="plugin-key",
            credential_issuer=self.issuer,
            auth_method=self.mode(),
            credential_status_required=True,
            authenticated_at=datetime.now(UTC),
        )

    def credential_sources(self):
        return (("plugin-key", self.issuer, self.store),)

    def security_dependencies(self):
        return (self.store,)


def test_public_authenticator_capability_binds_online_revocation(monkeypatch):
    # Factory 没有生产反注册接口；测试只修改注册表副本，退出后恢复原表。
    # pylint: disable=protected-access
    monkeypatch.setattr(AuthProducer, "_registry", dict(AuthProducer._registry))
    created = []

    @AuthProducer.register("sdk_contract_public")
    def build(config):
        authenticator = PublicPluginAuthenticator(KeyStoreProducer.dep(config, default="memory"))
        created.append(authenticator)
        return authenticator

    runtime = assemble_runtime(
        config={
            "security": {
                "default": {
                    "target": "standard",
                    "params": {"authenticator": {"target": "sdk_contract_public"}},
                }
            }
        }
    )
    try:
        actor = Scope(org="plugin", user="alice")
        store = created[0].store
        token = store.issue(actor, Role.USER)
        security = runtime.authenticate(Credentials(api_key=token))
        unit = runtime.api.add("public plugin", actor, security=security)[0]
        assert runtime.api.get(unit.id, actor, security=security).id == unit.id
        store.revoke(fingerprint(token))
        with pytest.raises(PermissionDeniedError):
            runtime.api.get(unit.id, actor, security=security)
    finally:
        runtime.close()


@pytest.mark.parametrize("runtime_name", ["default", "production"])
@pytest.mark.parametrize("named_authenticator", [False, True])
@pytest.mark.parametrize("with_runtime", [False, True])
def test_sdk_registers_actual_issuer_and_key_store(
    runtime_name, named_authenticator, with_runtime
) -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    auth_spec = {"target": "api_key", "params": {"root_api_key": "test-root"}}
    config = {
        "security": {
            runtime_name: {
                "target": "standard",
                "params": {"authenticator": "issuer" if named_authenticator else auth_spec},
            }
        }
    }
    if named_authenticator:
        config["authenticator"] = {"issuer": auth_spec}
    runtime = (
        assemble_runtime(config=config) if with_runtime else assemble_server_runtime(config=config)
    )
    api = runtime.api
    security_runtime = (
        runtime._kernel.security_runtime if with_runtime else runtime.security_runtime
    )
    authenticator = security_runtime.authenticator
    actor = Scope(org="acme", user="alice")
    token = authenticator.key_store.issue(actor, Role.USER)
    security = runtime.authenticate(Credentials(api_key=token))
    try:
        assert api._authorizer is security_runtime.authorizer
        store_key = (security.auth.credential_type, security.auth.credential_issuer)
        assert api._credentials._stores[store_key] is authenticator.key_store
        unit = api.add("private", actor, security=security)[0]
        assert api.get(unit.id, actor, security=security).content == "private"
        stranger_token = authenticator.key_store.issue(Scope(org="acme", user="bob"), Role.USER)
        stranger = runtime.authenticate(Credentials(api_key=stranger_token))
        with pytest.raises(PermissionDeniedError):
            api.get(unit.id, actor, security=stranger)
        authenticator.key_store.revoke(fingerprint(token))
        with pytest.raises(PermissionDeniedError):
            api.get(unit.id, actor, security=security)
    finally:
        runtime.close()


def test_memory_runtime_owns_security_lifecycle(monkeypatch) -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    config = {
        "security": {
            "default": {
                "target": "standard",
                "params": {"authenticator": {"target": "dev"}},
            }
        }
    }
    runtime = assemble_runtime(config=config)
    close = Mock()
    monkeypatch.setattr(type(runtime._kernel.security_runtime), "close", close)
    runtime.close()
    close.assert_called_once_with()


def test_rebinding_authenticator_removes_previous_issuer() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    config = {
        "security": {
            "default": {
                "target": "standard",
                "params": {"authenticator": {"target": "api_key"}},
            }
        }
    }
    runtime = assemble_runtime(config=config)
    try:
        auth = runtime._kernel.security_runtime.authenticator
        token = auth.key_store.issue(Scope(org="acme", user="alice"), Role.USER)
        security = new_request_context(
            auth.authenticate(Credentials(api_key=token)), surface=Surface.SDK
        )
        runtime.api._bind_credential_sources(build_dev_authenticator())
        with pytest.raises(ValidationError, match="CredentialStatusRegistry"):
            runtime.api.list(security.auth.actor, security=security)
    finally:
        runtime.close()


def test_explicit_pdp_cannot_disable_existing_router_governance(monkeypatch) -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    runtime = assemble_runtime()
    try:
        api = runtime.api
        monkeypatch.setattr(api, "_routing_enabled", lambda: True)
        with pytest.raises(ValidationError, match="空间治理"):
            api._bind_authorizer(api._authorizer)
    finally:
        runtime.close()
