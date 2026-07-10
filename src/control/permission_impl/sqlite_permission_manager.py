"""SQLite-backed :class:`~control.permission.PermissionManager`.

第一期真实 ACL 实现：

- `Scope()` 视为 platform admin，全局放行；
- owner 访问自己的 scope（含 agent/session 子 scope）默认放行；
- 跨 org 默认拒绝；
- grant 持久化到 SQLite，按 action 单行存储；
- revoke 采用软撤销（`revoked_at`）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from common.type_def import Scope
from control.base import ControlOperatorType
from control.permission import PermissionManager, PermissionProducer
from control.types import Action, Grant

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_org TEXT NOT NULL,
    grantor_user TEXT NOT NULL,
    grantor_agent TEXT NOT NULL,
    grantor_session TEXT NOT NULL,
    grantee_org TEXT NOT NULL,
    grantee_user TEXT NOT NULL,
    grantee_agent TEXT NOT NULL,
    grantee_session TEXT NOT NULL,
    action TEXT NOT NULL,
    expires_at TEXT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_check
ON grants (
    action,
    revoked_at,
    grantee_org,
    grantee_user,
    grantor_org,
    grantor_user
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _parse(dt: str | None) -> datetime | None:
    return None if not dt else datetime.fromisoformat(dt)


def _scope_tuple(scope: Scope) -> tuple[str, str, str, str]:
    return (scope.org, scope.user, scope.agent, scope.session)


def _owner_scope_covers(parent: Scope, child: Scope) -> bool:
    if parent.org != child.org or parent.user != child.user:
        return False
    if parent.agent and parent.agent != child.agent:
        return False
    if parent.session and parent.session != child.session:
        return False
    return True


class SQLitePermissionManager(PermissionManager):
    def __init__(self, db_path: str = ":memory:") -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.PERMISSION

    def health(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1")
        return None

    def grant(self, grant: Grant) -> None:
        now = _now()
        now_iso = _iso(now)
        with self._lock:
            for action in grant.actions:
                exists = self._conn.execute(
                    """
                    SELECT 1
                    FROM grants
                    WHERE grantor_org=? AND grantor_user=? AND grantor_agent=? AND grantor_session=?
                      AND grantee_org=? AND grantee_user=? AND grantee_agent=? AND grantee_session=?
                      AND action=? AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (
                        *_scope_tuple(grant.grantor),
                        *_scope_tuple(grant.grantee),
                        action.value,
                        now_iso,
                    ),
                ).fetchone()
                if exists is not None:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO grants (
                        grantor_org, grantor_user, grantor_agent, grantor_session,
                        grantee_org, grantee_user, grantee_agent, grantee_session,
                        action, expires_at, created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        *_scope_tuple(grant.grantor),
                        *_scope_tuple(grant.grantee),
                        action.value,
                        _iso(grant.expires_at),
                        _iso(now),
                    ),
                )

    def revoke(self, grant: Grant) -> None:
        with self._lock:
            for action in grant.actions:
                self._conn.execute(
                    """
                    UPDATE grants
                    SET revoked_at=?
                    WHERE grantor_org=? AND grantor_user=? AND grantor_agent=? AND grantor_session=?
                      AND grantee_org=? AND grantee_user=? AND grantee_agent=? AND grantee_session=?
                      AND action=? AND revoked_at IS NULL
                    """,
                    (
                        _iso(_now()),
                        *_scope_tuple(grant.grantor),
                        *_scope_tuple(grant.grantee),
                        action.value,
                    ),
                )

    def check(self, actor: Scope, target: Scope, action: Action) -> bool:
        if actor == Scope():
            return True

        if _owner_scope_covers(actor, target):
            return True

        if actor.org != target.org:
            return False

        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    1
                FROM grants
                WHERE action=?
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND grantee_org=?
                  AND grantee_user=?
                  AND (grantee_agent='' OR grantee_agent=?)
                  AND (grantee_session='' OR grantee_session=?)
                  AND grantor_org=?
                  AND grantor_user=?
                  AND (grantor_agent='' OR grantor_agent=?)
                  AND (grantor_session='' OR grantor_session=?)
                LIMIT 1
                """,
                (
                    action.value,
                    _iso(_now()),
                    actor.org,
                    actor.user,
                    actor.agent,
                    actor.session,
                    target.org,
                    target.user,
                    target.agent,
                    target.session,
                ),
            ).fetchone()
        return row is not None


@PermissionProducer.register("sqlite")
def _build(config):
    db_path = config.get("db_path", ":memory:")
    return SQLitePermissionManager(str(db_path))
