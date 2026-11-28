"""Install-scoped local tokens (F17 D3): durable, revocable, one valid digest per install."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import (
    AuthenticationError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.security.authentication.authentication_impl.local_token_authenticator import (  # noqa: E501
    LocalTokenAuthenticator,
    LocalTokenStore,
    issue_token,
    token_digest,
    validate_install_principal,
)
from jiuwen_memory.common.security.types import Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit

SWARM_ACTOR = Scope(org="acme", user="swarm", agent="jiuwenswarm-install")

STRIPED_ACTOR = Scope(org="acme", agent="codex-install")


def _authenticator(tmp_path) -> LocalTokenAuthenticator:
    return LocalTokenAuthenticator(LocalTokenStore(str(tmp_path / "tokens.db")))


def test_valid_token_resolves_to_registered_actor(tmp_path):
    auth = _authenticator(tmp_path)
    token = issue_token()
    auth.store.register("install-1", token_digest(token), STRIPED_ACTOR, Role.USER)
    context = auth.authenticate(Credentials(api_key=token))
    assert context.actor == STRIPED_ACTOR
    assert context.role is Role.USER
    assert context.credential_type == "local_token"
    assert context.credential_id == token_digest(token)
    assert context.auth_method == "local_token"
    assert context.credential_status_required is False


def test_invalid_and_revoked_tokens_are_rejected(tmp_path):
    auth = _authenticator(tmp_path)
    token = issue_token()
    auth.store.register("install-1", token_digest(token), STRIPED_ACTOR, Role.USER)
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=issue_token()))
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=""))
    auth.store.revoke("install-1")
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=token))


def test_reregister_keeps_at_most_one_valid_digest(tmp_path):
    auth = _authenticator(tmp_path)
    first, second = issue_token(), issue_token()
    auth.store.register("install-1", token_digest(first), STRIPED_ACTOR, Role.USER)
    auth.store.register("install-1", token_digest(second), STRIPED_ACTOR, Role.USER)
    with pytest.raises(AuthenticationError):
        auth.authenticate(Credentials(api_key=first))
    assert auth.authenticate(Credentials(api_key=second)).actor == STRIPED_ACTOR
    assert auth.store.digest_for("install-1") == token_digest(second)


def test_token_survives_process_restart(tmp_path):
    token = issue_token()
    first = LocalTokenStore(str(tmp_path / "tokens.db"))
    first.register("install-1", token_digest(token), STRIPED_ACTOR, Role.USER)
    first.close()
    second = LocalTokenAuthenticator(LocalTokenStore(str(tmp_path / "tokens.db")))
    assert second.authenticate(Credentials(api_key=token)).actor == STRIPED_ACTOR


def test_migrated_user_and_agent_principal_is_accepted():
    actor = Scope(org="acme", user="alice", agent="swarm-1")
    validate_install_principal(actor, Role.USER)


def test_subject_rules_reject_project_axes_and_root(tmp_path):
    with pytest.raises(ValidationError):
        validate_install_principal(Scope(org="acme", space="p", agent="a"), Role.USER)
    with pytest.raises(ValidationError):
        validate_install_principal(Scope(org="acme", session="s", agent="a"), Role.USER)
    with pytest.raises(PermissionDeniedError):
        validate_install_principal(Scope(org="acme", agent="a"), Role.ROOT)
    with pytest.raises(ValidationError):
        LocalTokenStore(str(tmp_path / "tokens.db")).register(
            "install-1", token_digest(issue_token()), Scope(org="acme", space="p"), Role.USER
        )


def test_principal_lookup_reports_revocation(tmp_path):
    auth = _authenticator(tmp_path)
    auth.store.register("install-1", token_digest(issue_token()), SWARM_ACTOR, Role.USER)
    principal = auth.store.principal_for_install("install-1")
    assert principal.actor == SWARM_ACTOR and principal.revoked is False
    auth.store.revoke("install-1")
    assert auth.store.principal_for_install("install-1").revoked is True
    assert auth.store.principal_for_install("missing") is None


def test_issued_token_is_256_bit_and_digest_is_sha256():
    token = issue_token()
    assert len(token) >= 43, "token is shorter than 256 bits of entropy"
    assert len(token_digest(token)) == 64
