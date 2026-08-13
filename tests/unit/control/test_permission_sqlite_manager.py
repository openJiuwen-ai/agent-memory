from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.permission_impl import sqlite_permission_manager
from jiuwen_memory.control.permission_impl.sqlite_permission_manager import SQLitePermissionManager
from jiuwen_memory.control.types import Action, Grant, PermissionContext

pytestmark = pytest.mark.unit


def test_owner_scope_is_allowed_without_grant(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    actor = Scope(org="acme", user="alice")

    assert mgr.check(actor, actor, Action.READ) is True
    assert mgr.check(actor, actor, Action.WRITE) is True


def test_cross_org_is_denied_by_default(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))

    actor = Scope(org="acme", user="alice")
    target = Scope(org="other", user="bob")

    assert mgr.check(actor, target, Action.READ) is False


def test_cross_space_is_denied_by_default(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))

    actor = Scope(org="acme", space="product", user="alice")
    target = Scope(org="acme", space="coding", user="alice")

    assert mgr.check(actor, target, Action.READ) is False


def test_cross_space_explicit_grant_allows_action(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    grantor = Scope(org="acme", space="product", user="owner")
    grantee = Scope(org="acme", space="coding", user="reader")
    target = Scope(org="acme", space="product", user="owner", agent="agent-a")

    mgr.grant(Grant(grantor=grantor, grantee=grantee, actions=[Action.READ]))

    assert mgr.check(grantee, target, Action.READ) is True
    assert mgr.check(
        Scope(org="acme", space="other", user="reader"),
        target,
        Action.READ,
    ) is False


def test_agent_user_principal_path_changes_owner_cover(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    context = PermissionContext(metadata={"principal_path": "agent_user"})
    actor = Scope(org="acme", space="coding", agent="agent-a")
    target = Scope(org="acme", space="coding", agent="agent-a", user="alice")

    assert mgr.check(actor, target, Action.READ, context=context) is True
    assert mgr.check(actor, target, Action.READ) is False


def test_grant_persists_and_allows_action(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    grantor = Scope(org="acme", user="owner")
    grantee = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner", agent="agent-a")
    grant = Grant(grantor=grantor, grantee=grantee, actions=[Action.READ])

    mgr.grant(grant)

    reloaded = SQLitePermissionManager(str(db_path))
    assert reloaded.check(grantee, target, Action.READ) is True
    assert reloaded.check(grantee, target, Action.WRITE) is False


def test_revoke_soft_disables_existing_grant(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    grantor = Scope(org="acme", user="owner")
    grantee = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner", session="session-a")
    grant = Grant(grantor=grantor, grantee=grantee, actions=[Action.READ])

    mgr.grant(grant)
    assert mgr.check(grantee, target, Action.READ) is True

    mgr.revoke(grant)

    reloaded = SQLitePermissionManager(str(db_path))
    assert reloaded.check(grantee, target, Action.READ) is False


def test_expired_grant_is_rejected(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    grant = Grant(
        grantor=Scope(org="acme", user="owner"),
        grantee=Scope(org="acme", user="reader"),
        actions=[Action.READ],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    mgr.grant(grant)

    assert mgr.check(
        Scope(org="acme", user="reader"),
        Scope(org="acme", user="owner", session="session-a"),
        Action.READ,
    ) is False


def test_expired_grant_does_not_block_regrant(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    grantor = Scope(org="acme", user="owner")
    grantee = Scope(org="acme", user="reader")
    target = Scope(org="acme", user="owner")

    mgr.grant(
        Grant(
            grantor=grantor,
            grantee=grantee,
            actions=[Action.READ],
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    mgr.grant(
        Grant(
            grantor=grantor,
            grantee=grantee,
            actions=[Action.READ],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )

    assert mgr.check(grantee, target, Action.READ) is True


def test_platform_admin_has_global_access(tmp_path) -> None:
    db_path = tmp_path / "permission.db"
    mgr = SQLitePermissionManager(str(db_path))
    admin = Scope()
    target = Scope(org="acme", user="owner")

    assert mgr.check(admin, target, Action.READ) is True
    assert mgr.check(admin, target, Action.DELETE) is True


def test_sqlite_permission_manager_creates_parent_directory(tmp_path) -> None:
    db_path = tmp_path / "missing" / "nested" / "permission.db"

    mgr = SQLitePermissionManager(str(db_path))

    assert db_path.exists()
    assert mgr.health() is None


def test_sqlite_permission_manager_migrates_legacy_grants_before_indexes(tmp_path) -> None:
    db_path = tmp_path / "legacy-permission.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grants (
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
        """
    )
    conn.close()

    manager = SQLitePermissionManager(str(db_path))
    grantor = Scope(org="acme", space="coding", user="owner")
    grantee = Scope(org="acme", space="coding", user="reader")
    manager.grant(Grant(grantor=grantor, grantee=grantee, actions=[Action.READ]))

    assert manager.check(grantee, grantor, Action.READ) is True


def test_check_pushes_scope_matching_into_sql(tmp_path, monkeypatch) -> None:
    class _ConnectionSpy:
        def __init__(self, conn) -> None:
            self._conn = conn
            self.grant_selects: list[str] = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT") and "FROM grants" in normalized:
                self.grant_selects.append(normalized)
            return self._conn.execute(sql, params)

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    db_path = tmp_path / "permission.db"
    original_connect = sqlite_permission_manager.sqlite3.connect
    spies: list[_ConnectionSpy] = []

    def connect(*args, **kwargs):
        spy = _ConnectionSpy(original_connect(*args, **kwargs))
        spies.append(spy)
        return spy

    monkeypatch.setattr(sqlite_permission_manager.sqlite3, "connect", connect)
    mgr = SQLitePermissionManager(str(db_path))
    grant = Grant(
        grantor=Scope(org="acme", user="owner"),
        grantee=Scope(org="acme", user="reader"),
        actions=[Action.READ],
    )
    mgr.grant(grant)
    spy = spies[0]
    spy.grant_selects.clear()

    assert mgr.check(
        Scope(org="acme", user="reader", agent="agent-a"),
        Scope(org="acme", user="owner", session="session-a"),
        Action.READ,
    ) is True

    assert len(spy.grant_selects) == 1
    query = spy.grant_selects[0]
    assert "grantee_org=?" in query
    assert "grantor_org=?" in query
    assert "grantee_user=?" not in query
