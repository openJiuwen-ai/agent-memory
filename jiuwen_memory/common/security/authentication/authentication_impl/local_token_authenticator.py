# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""本机令牌认证（F17 D3）：安装级高熵令牌，服务端只存摘要。

与 ``api_key`` 的差别：令牌 256 位随机、不进入 Argon2id（高熵随机值不需要慢哈希），
登记在 SQLite 里所以进程重启后仍有效；摘要按安装 ID 唯一，撤销在认证路径上按请求
检查，不依赖 PEP 的在线撤销注册表。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jiuwen_memory.common.errors import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope

_METHOD = "local_token"
_CREDENTIAL = "local_token"
_FAILED = "authentication failed"
_TOKEN_BYTES = 32
_TABLE = """
CREATE TABLE IF NOT EXISTS install_tokens (
    install_id  TEXT PRIMARY KEY,
    digest      TEXT NOT NULL UNIQUE,
    actor       TEXT NOT NULL,
    role        TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
)
"""


def issue_token() -> str:
    """A 256-bit random install token (URL-safe, no padding)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_digest(token: str) -> str:
    """SHA-256 of the presented token; the only form the server stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstallPrincipal:
    """A registered install identity, including revoked ones (recovery needs them)."""

    actor: Scope
    role: Role
    revoked: bool


def validate_install_principal(
    actor: Scope, role: Role, *, allow_scope: bool = False
) -> None:
    """Subject rules for one install (F17 D3).

    Wider than :func:`validate_principal_registration`: an install may be migrated
    from JiuwenSwarm with both ``user`` and ``agent`` set (the old full actor must be
    preserved or task ownership and connection matching break). ``org`` is required,
    project axes (space/session) are not allowed, and ROOT cannot be registered.
    """
    if not isinstance(actor, Scope):
        raise ValidationError("install registration requires a Scope")
    values = (actor.org, actor.space, actor.user, actor.agent, actor.session)
    if any(not isinstance(value, str) or value != value.strip() for value in values):
        raise ValidationError("install registration requires canonical string identity fields")
    if not actor.org:
        raise ValidationError("install registration requires an org")
    if not allow_scope and (actor.space or actor.session):
        raise ValidationError("install registration must not carry space or session")
    if not actor.user and not actor.agent:
        raise ValidationError("install registration requires a user or an agent dimension")
    if not isinstance(role, Role):
        raise ValidationError("install registration requires a Role")
    if role is Role.ROOT:
        raise PermissionDeniedError("register_install", message="cannot register ROOT")


class LocalTokenStore:
    """SQLite token registry: at most one valid digest per install id."""

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValidationError("local token store requires a database path")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(_TABLE)

    def register(
        self,
        install_id: str,
        digest: str,
        actor: Scope,
        role: Role,
        *,
        allow_scope: bool = False,
    ) -> None:
        """Register (or re-activate) one install, revoking its previous digest first.

        ``allow_scope`` is reserved for migration (D12), where the verified legacy actor
        carries project/session dimensions needed to match existing task ownership and
        bindings; ordinary installs must keep those empty.
        """
        if not install_id or not digest:
            raise ValidationError("install id and token digest are required")
        validate_install_principal(actor, role, allow_scope=allow_scope)
        stamp = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {
                "org": actor.org,
                "space": actor.space,
                "user": actor.user,
                "agent": actor.agent,
                "session": actor.session,
            },
            sort_keys=True,
        )
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE install_tokens SET revoked = 1 "
                    "WHERE install_id = ? AND digest <> ?",
                    (install_id, digest),
                )
                self._conn.execute(
                    "INSERT INTO install_tokens "
                    "(install_id, digest, actor, role, revoked, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?) "
                    "ON CONFLICT(install_id) DO UPDATE SET "
                    "digest = excluded.digest, actor = excluded.actor, "
                    "role = excluded.role, revoked = 0, updated_at = excluded.updated_at",
                    (install_id, digest, payload, role.value, stamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "install_token", message="token digest is registered to another install"
            ) from exc

    def revoke(self, install_id: str) -> None:
        """Revoke an install's token; re-registering the same install re-activates it."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE install_tokens SET revoked = 1, updated_at = ? WHERE install_id = ?",
                (datetime.now(UTC).isoformat(), install_id),
            )

    def purge(self, install_id: str) -> None:
        """Delete a revoked one-off credential row (e.g. the space-create admin token)."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM install_tokens WHERE install_id = ? AND revoked = 1",
                (install_id,),
            )

    def digest_for(self, install_id: str) -> str | None:
        """The digest currently registered for ``install_id`` (revoked or not)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT digest FROM install_tokens WHERE install_id = ?", (install_id,)
            ).fetchone()
        return row[0] if row else None

    def install_for_digest(self, digest: str) -> str | None:
        """The install that owns a credential digest (request attribution for hooks)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT install_id FROM install_tokens WHERE digest = ?", (digest,)
            ).fetchone()
        return row[0] if row else None

    def principal_for_install(self, install_id: str) -> InstallPrincipal | None:
        """The principal behind an install, including whether its token was revoked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT actor, role, revoked FROM install_tokens WHERE install_id = ?",
                (install_id,),
            ).fetchone()
        if row is None:
            return None
        payload, role_name, revoked = row
        return InstallPrincipal(
            actor=Scope(**json.loads(payload)), role=Role(role_name), revoked=bool(revoked)
        )

    def resolve(self, token: str) -> AuthContext | None:
        """Resolve a presented token to a trusted context; ``None`` when invalid."""
        if not token:
            return None
        computed = token_digest(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT digest, actor, role, revoked FROM install_tokens WHERE digest = ?",
                (computed,),
            ).fetchone()
        if row is None:
            return None
        stored, payload, role_name, revoked = row
        if not hmac.compare_digest(str(stored), computed) or revoked:
            return None
        return AuthContext(
            actor=Scope(**json.loads(payload)),
            role=Role(role_name),
            credential_type=_CREDENTIAL,
            credential_id=computed,
            auth_method=_METHOD,
            # Revocation is checked on every resolve, so the PEP needs no second lookup.
            credential_status_required=False,
            authenticated_at=datetime.now(UTC),
        )

    def health(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class LocalTokenAuthenticator(Authenticator):
    """Bearer token authentication against :class:`LocalTokenStore`."""

    def __init__(self, store: LocalTokenStore, name: str = "default") -> None:
        self._store = store
        self._name = name

    @property
    def store(self) -> LocalTokenStore:
        return self._store

    def authenticate(self, credentials: Credentials) -> AuthContext:
        context = self._store.resolve(credentials.api_key)
        if context is None:
            raise AuthenticationError(_FAILED)
        if self._name and context.credential_issuer != self._name:
            # Issuer is diagnostic only; the store owns the token namespace.
            return AuthContext(
                actor=context.actor,
                role=context.role,
                credential_type=context.credential_type,
                credential_id=context.credential_id,
                auth_method=context.auth_method,
                credential_issuer=self._name,
                credential_status_required=False,
                authenticated_at=context.authenticated_at,
            )
        return context

    def mode(self) -> str:
        return _METHOD

    def requires_loopback_binding(self) -> bool:
        return True

    def requires_concurrency_guard(self) -> bool:
        return False

    def security_dependencies(self) -> tuple[object, ...]:
        return (self._store,)

    def health(self) -> None:
        self._store.health()


@AuthProducer.register("local_token")
def _build(config):
    db_path = str(config.get("db_path", "")).strip()
    if not db_path:
        raise ValidationError("local_token requires a durable db_path")
    return LocalTokenAuthenticator(LocalTokenStore(db_path), name=config.name)
