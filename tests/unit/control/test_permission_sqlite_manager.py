from __future__ import annotations

from datetime import datetime, timedelta, timezone

from common.type_def import Scope
from control.permission_impl import sqlite_permission_manager
from control.permission_impl.sqlite_permission_manager import SQLitePermissionManager
from control.types import Action, Grant


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
    assert "LIMIT 1" in query
