# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK 与 Server 同源装配，撤销前签发的上下文也须逐请求失效。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from jiuwen_memory.api import (
    Credentials,
    PermissionDeniedError,
    Scope,
    Surface,
    ValidationError,
    assemble,
    assemble_runtime,
    build_configured_security_runtime,
    build_dev_authenticator,
    new_request_context,
)
from jiuwen_memory.common.security.authentication.key_store import fingerprint
from jiuwen_memory.common.security.types import Role

pytestmark = pytest.mark.unit


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
    runtime = assemble_runtime(config=config) if with_runtime else None
    api = runtime.api if runtime is not None else assemble(config=config)
    security_runtime = build_configured_security_runtime(config)
    authenticator = security_runtime.authenticator
    actor = Scope(org="acme", user="alice")
    token = authenticator.key_store.issue(actor, Role.USER)
    security = new_request_context(
        authenticator.authenticate(Credentials(api_key=token)), surface=Surface.SDK
    )
    try:
        assert api._authorizer is security_runtime.authorizer
        store_key = (security.auth.credential_type, security.auth.credential_issuer)
        assert api._credentials._stores[store_key] is authenticator.key_store
        unit = api.add("private", actor, security=security)[0]
        assert api.get(unit.id, actor, security=security).content == "private"
        stranger_token = authenticator.key_store.issue(Scope(org="acme", user="bob"), Role.USER)
        stranger = new_request_context(
            authenticator.authenticate(Credentials(api_key=stranger_token)), surface=Surface.SDK
        )
        with pytest.raises(PermissionDeniedError):
            api.get(unit.id, actor, security=stranger)
        authenticator.key_store.revoke(fingerprint(token))
        with pytest.raises(PermissionDeniedError):
            api.get(unit.id, actor, security=security)
    finally:
        if runtime is not None:
            runtime.close()
        else:
            api._ingest_jobs.close()
            security_runtime.close()


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
    monkeypatch.setattr(type(runtime._security_runtime), "close", close)
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
        auth = runtime._security_runtime.authenticator
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
