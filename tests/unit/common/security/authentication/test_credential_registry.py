"""CredentialStatusRegistry：PEP 持有的凭据撤销在线复核入口（F05 §认证不变量 6）。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.authentication.credential_registry import (
    CredentialStatusRegistry,
)
from jiuwen_memory.common.security.authentication.key_store import PrincipalKeyStore
from jiuwen_memory.common.security.types import AuthContext, Role
from jiuwen_memory.common.type_def.scope import Scope

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
    credential_issuer: str = "default",
    credential_status_required: bool = True,
) -> AuthContext:
    return AuthContext(
        actor=Scope(org="acme", user="alice"),
        credential_type=credential_type,
        credential_id=credential_id,
        credential_issuer=credential_issuer,
        credential_status_required=credential_status_required,
    )


def test_is_revoked_routes_to_the_registered_store() -> None:
    reg = CredentialStatusRegistry()
    reg.register("api_key", "default", _StubStore({"revoked-1"}))
    assert reg.is_revoked(_auth(credential_type="api_key", credential_id="revoked-1")) is True
    assert reg.is_revoked(_auth(credential_type="api_key", credential_id="active-1")) is False


def test_parallel_issuers_keep_independent_revocation_truth_sources() -> None:
    reg = CredentialStatusRegistry()
    reg.register("api_key", "primary", _StubStore({"same-id"}))
    reg.register("api_key", "partner", _StubStore())
    assert reg.is_revoked(
        _auth(
            credential_type="api_key",
            credential_id="same-id",
            credential_issuer="primary",
        )
    )
    assert not reg.is_revoked(
        _auth(
            credential_type="api_key",
            credential_id="same-id",
            credential_issuer="partner",
        )
    )


def test_required_status_with_unregistered_issuer_fails_closed() -> None:
    """声明需要在线复核的凭据，未注册 issuer 必须 fail-closed。"""
    reg = CredentialStatusRegistry()
    with pytest.raises(ValidationError, match="未注册到 CredentialStatusRegistry"):
        reg.is_revoked(_auth(credential_type="api_key", credential_id="key-1"))


def test_required_status_without_credential_id_fails_closed() -> None:
    reg = CredentialStatusRegistry()
    reg.register("api_key", "default", _StubStore())
    with pytest.raises(ValidationError, match="缺少 credential_id"):
        reg.is_revoked(_auth(credential_type="api_key", credential_id=""))


def test_non_revocable_credential_keeps_id_without_status_lookup() -> None:
    """ROOT/trusted 的 id 用于审计和绑定，不应被误当成撤销 Store capability。"""
    reg = CredentialStatusRegistry()
    auth = _auth(
        credential_type="gateway",
        credential_id="audit-fingerprint",
        credential_issuer="",
        credential_status_required=False,
    )
    assert reg.is_revoked(auth) is False


def test_health_rejects_store_without_is_revoked_override() -> None:
    """注册了未覆盖 is_revoked 的 Store，启动期 fail-closed（P1-3）。"""
    reg = CredentialStatusRegistry()
    reg.register("api_key", "default", _StoreWithoutRevocation())
    with pytest.raises(ValidationError):
        reg.health()


def test_health_passes_when_store_overrides_is_revoked() -> None:
    reg = CredentialStatusRegistry()
    reg.register("api_key", "default", _StubStore())
    assert reg.health() is None
