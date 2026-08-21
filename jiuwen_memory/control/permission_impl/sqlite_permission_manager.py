"""SQLite-backed :class:`~control.permission.PermissionManager`.

第一期真实 ACL 实现：

- `Scope()` 视为 platform admin，全局放行；
- owner 访问自己的 scope（含 agent/session 子 scope）默认放行；
- 跨 org 默认拒绝，同 org 跨 space 默认拒绝；
- grant 持久化到 SQLite，按 action 单行存储；
- revoke 采用软撤销（`revoked_at`）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.permission import PermissionManager, PermissionProducer
from jiuwen_memory.control.types import Action, Grant, PermissionContext

_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_org TEXT NOT NULL,
    grantor_space TEXT NOT NULL,
    grantor_user TEXT NOT NULL,
    grantor_agent TEXT NOT NULL,
    grantor_session TEXT NOT NULL,
    grantee_org TEXT NOT NULL,
    grantee_space TEXT NOT NULL,
    grantee_user TEXT NOT NULL,
    grantee_agent TEXT NOT NULL,
    grantee_session TEXT NOT NULL,
    action TEXT NOT NULL,
    expires_at TEXT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT NULL
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_grants_check
ON grants (
    action,
    revoked_at,
    grantee_org,
    grantee_space,
    grantee_user,
    grantor_org,
    grantor_space,
    grantor_user
)
;
CREATE INDEX IF NOT EXISTS idx_grants_check_scope
ON grants (
    action,
    revoked_at,
    grantee_org,
    grantee_space,
    grantor_org,
    grantor_space
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _scope_tuple(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _principal_order(context: PermissionContext | None) -> tuple[str, str, str]:
    if context and context.metadata.get("principal_path") == "agent_user":
        return ("agent", "user", "session")
    return ("user", "agent", "session")


def _owner_scope_covers(
    parent: Scope,
    child: Scope,
    context: PermissionContext | None = None,
) -> bool:
    if parent == Scope():
        return True
    if parent.org != child.org or parent.space != child.space:
        return False

    order = _principal_order(context)
    primary = order[0]
    if getattr(parent, primary) != getattr(child, primary):
        return False

    for index, dim in enumerate(order[1:], start=1):
        parent_value = getattr(parent, dim)
        child_value = getattr(child, dim)
        if parent_value:
            if parent_value != child_value:
                return False
            continue
        if any(getattr(parent, later) for later in order[index + 1:]):
            return False
        return True
    return True


def _row_scope(row: sqlite3.Row | tuple, prefix: str) -> Scope:
    if isinstance(row, sqlite3.Row):
        return Scope(
            org=row[f"{prefix}_org"],
            space=row[f"{prefix}_space"],
            user=row[f"{prefix}_user"],
            agent=row[f"{prefix}_agent"],
            session=row[f"{prefix}_session"],
        )
    offset = 0 if prefix == "grantor" else 5
    return Scope(
        org=row[offset],
        space=row[offset + 1],
        user=row[offset + 2],
        agent=row[offset + 3],
        session=row[offset + 4],
    )


class SQLitePermissionManager(PermissionManager):
    def __init__(self, db_path: str = ":memory:") -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_TABLE_SCHEMA)
            self._migrate_schema()
            self._conn.executescript(_INDEX_SCHEMA)

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
                    WHERE grantor_org=? AND grantor_space=? AND grantor_user=?
                      AND grantor_agent=? AND grantor_session=?
                      AND grantee_org=? AND grantee_space=? AND grantee_user=?
                      AND grantee_agent=? AND grantee_session=?
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
                        grantor_org, grantor_space, grantor_user,
                        grantor_agent, grantor_session,
                        grantee_org, grantee_space, grantee_user,
                        grantee_agent, grantee_session,
                        action, expires_at, created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
                    WHERE grantor_org=? AND grantor_space=? AND grantor_user=?
                      AND grantor_agent=? AND grantor_session=?
                      AND grantee_org=? AND grantee_space=? AND grantee_user=?
                      AND grantee_agent=? AND grantee_session=?
                      AND action=? AND revoked_at IS NULL
                    """,
                    (
                        _iso(_now()),
                        *_scope_tuple(grant.grantor),
                        *_scope_tuple(grant.grantee),
                        action.value,
                    ),
                )

    def check(
        self,
        actor: Scope,
        target: Scope,
        action: Action,
        context: PermissionContext | None = None,
    ) -> bool:
        if actor == Scope():
            return True

        if _owner_scope_covers(actor, target, context):
            return True

        if actor.org != target.org:
            return False

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    grantor_org,
                    grantor_space,
                    grantor_user,
                    grantor_agent,
                    grantor_session,
                    grantee_org,
                    grantee_space,
                    grantee_user,
                    grantee_agent,
                    grantee_session
                FROM grants
                WHERE action=?
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND grantee_org=?
                  AND grantor_org=?
                """,
                (
                    action.value,
                    _iso(_now()),
                    actor.org,
                    target.org,
                ),
            ).fetchall()
        for row in rows:
            grantee = _row_scope(row, "grantee")
            grantor = _row_scope(row, "grantor")
            if _owner_scope_covers(grantee, actor, context) and _owner_scope_covers(
                grantor, target, context
            ):
                return True
        return False

    def _migrate_schema(self) -> None:
        columns: set[str] = set()
        rows = self._conn.execute("PRAGMA table_info(grants)").fetchall()
        for row in rows:
            column_name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            columns.add(column_name)
        for column in ("grantor_space", "grantee_space"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE grants ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )


@PermissionProducer.register("sqlite")
def _build(config):
    db_path = config.get("db_path", ":memory:")
    return SQLitePermissionManager(str(db_path))
