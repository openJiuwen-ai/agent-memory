"""CredentialStatusRegistry：PEP 持有的凭据撤销在线复核入口（F05 §认证不变量 6）。"""

from __future__ import annotations

import pytest

from common.errors import ValidationError
from common.security.authentication.credential_registry import CredentialStatusRegistry
from common.security.authentication.key_store import PrincipalKeyStore
from common.security.types import AuthContext, Role
from common.type_def.scope import Scope

pytestmark = pytest.mark.unit


class _StubStore(PrincipalKeyStore):
    """覆盖 is_revoked 的可撤销 Store 桩。"""

    def __init__(self, revoked_ids: set[str] | None = None) -> None:
        self._revoked = revoked_ids or set()

    def issue(self, actor: Scope, role: Role) -> str:
        raise NotImplementedError

    def resolve(self, api_key: str) -> AuthContext | None:
        raise NotImplementedError

    def revoke(self, key_fp: str) -> None:
        self._revoked.add(key_fp)

    def get_role(self, actor: Scope) -> Role | None:
        return Role.USER

    def is_revoked(self, credential_id: str) -> bool:
        return credential_id in self._revoked

    def health(self) -> None:
        return None


class _StoreWithoutRevocation(PrincipalKeyStore):
    """漏实现 is_revoked 的桩（继承默认 NotImplementedError）。"""

    def issue(self, actor: Scope, role: Role) -> str:
        raise NotImplementedError

    def resolve(self, api_key: str) -> AuthContext | None:
        return None

    def revoke(self, key_fp: str) -> None:
        return None

    def get_role(self, actor: Scope) -> Role | None:
        return Role.USER

    def health(self) -> None:
        return None


def _auth(
    *,
    credential_type: str,
    credential_id: str,
    auth_method: str = "default",
    credential_issuer: str = "default",
) -> AuthContext:
    """Round4 P1-4: 添加 credential_issuer 参数，默认值与 auth_method 一致。"""
    return AuthContext(
        actor=Scope(org="acme", user="alice"),
        credential_type=credential_type,
        credential_id=credential_id,
        auth_method=auth_method,
        credential_issuer=credential_issuer,
    )


def test_is_revoked_routes_to_the_registered_store() -> None:
    reg = CredentialStatusRegistry()
    # Round3: register 需要 (credential_type, authenticator_name, store)
    reg.register("api_key", "default", _StubStore({"revoked-1"}))
    assert reg.is_revoked(_auth(credential_type="api_key", credential_id="revoked-1")) is True
    assert reg.is_revoked(_auth(credential_type="api_key", credential_id="active-1")) is False


def test_unregistered_credential_type_is_not_revoked() -> None:
    """Round7 P1-3: 未注册的 issuer 必须 fail-closed（抛出 ValidationError）。"""
    reg = CredentialStatusRegistry()
    with pytest.raises(ValidationError, match="未注册到 CredentialStatusRegistry"):
        reg.is_revoked(_auth(credential_type="dev", credential_id="dev-1"))


def test_empty_credential_id_is_not_revoked() -> None:
    reg = CredentialStatusRegistry()
    # Round3: register 需要 (credential_type, authenticator_name, store)
    reg.register("api_key", "default", _StubStore())
    assert reg.is_revoked(_auth(credential_type="api_key", credential_id="")) is False


def test_health_rejects_store_without_is_revoked_override() -> None:
    """注册了未覆盖 is_revoked 的 Store，启动期 fail-closed（P1-3）。"""
    reg = CredentialStatusRegistry()
    # Round3: register 需要 (credential_type, authenticator_name, store)
    reg.register("api_key", "default", _StoreWithoutRevocation())
    with pytest.raises(ValidationError):
        reg.health()


def test_health_passes_when_store_overrides_is_revoked() -> None:
    reg = CredentialStatusRegistry()
    # Round3: register 需要 (credential_type, authenticator_name, store)
    reg.register("api_key", "default", _StubStore())
    assert reg.health() is None
