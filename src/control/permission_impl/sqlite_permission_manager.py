"""SQLite-backed :class:`~control.permission.PermissionManager`.

第一期真实 ACL 实现：

- `Scope()` 视为 platform admin，全局放行（**仅在无认证上下文时**，见下）；
- owner 访问自己的 scope（含 agent/session 子 scope）默认放行；
- 跨 org 默认拒绝，同 org 跨 space 默认拒绝；
- grant 持久化到 SQLite，按 action 单行存储；
- revoke 采用软撤销（`revoked_at`）。

传入 ``auth``（认证层产出的 ``AuthContext``）时另加三条，判定顺序即代码顺序：

1. ``auth.actor`` 与 ``actor`` 不一致 → 拒；
2. 管理面资源（``resource_type`` 为 admin/audit，或 space 的写/删）要求 ROOT；
3. ROOT 按 **role** 判定（§3.5）。

此时空 `Scope()` **不再**自动等于 platform admin：特权必须来自认证层的显式结论。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from common.type_def import Scope
from common.security.types import AuthContext, Role
from control.base import ControlOperatorType
from control.permission import PermissionManager, PermissionProducer
from control.types import Action, Grant, PermissionContext

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
        later_start = index + 1
        if any(getattr(parent, later) for later in order[later_start:]):
            return False
        return True
    return True


_MANAGEMENT_RESOURCES = frozenset({"admin", "audit"})
_SPACE_LIFECYCLE_ACTIONS = frozenset({Action.WRITE, Action.DELETE})


def _management_plane_denies(
    auth: AuthContext | None,
    action: Action,
    context: PermissionContext | None,
) -> bool:
    """管理面（§3.2 的 ROOT 行）要求 ROOT，非 ROOT 一律拒。

    「这是不是管理操作」由 ``PermissionContext.resource_type`` 说了算，而不是由
    「target 恰好是空 Scope」间接表达。后者是**靠数据形状表达语义**：把 target
    填成自己的 scope 就绕过去了，而调用方是能控制 target 的。

    ``grant`` / ``revoke`` **不在**这张表里。§3.2 那行说的是「**跨租户**修改权限」，
    而跨 org 的 grant 今天已被 ``actor.org != target.org`` 挡住；对自己 scope 发
    grant 是 Grant 模型的主用途，把它闸进 ROOT 会废掉正常共享。

    ``auth`` 为 ``None`` 时不闸——那是没有认证上下文的场景（后台 job / 单测 /
    ``build_kernel`` 直连），此时无从判定角色，沿用旧的 ACL 判定。
    """
    if auth is None or context is None:
        return False
    if auth.role is Role.ROOT:
        return False
    if context.resource_type in _MANAGEMENT_RESOURCES:
        return True
    # 创建/删除租户属 ROOT（§3.2）。同为 resource_type="space" 的 get/update/archive
    # 走 READ/UPDATE，不在此列——§3.2 只点名了「创建/删除」，读 space 元数据若也要
    # ROOT，普通用户连自己所在 space 的名字都拿不到。
    return context.resource_type == "space" and action in _SPACE_LIFECYCLE_ACTIONS


# agent 代 user 操作的判定路径已删除。它依赖网关 header 送来的 ``acting_user``，而
# header 只能证明网关声称某个 user，证明不了该 user 真的授权了这个 agent（F05
# §从 header 直接产生 Delegation）。代操作现在走 ``DelegationStore`` 里的服务端记录，
# 由 ``StandardAuthorizer`` 按 ``delegation_id`` 复核；可委托动作的 allowlist 迁到
# ``common.security.types.DELEGATABLE_ACTIONS``。


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
        *,
        auth: AuthContext | None = None,
    ) -> bool:
        if auth is not None and auth.actor != actor:
            # 两个身份来源不一致：要么是接线错误，要么是拿 A 的凭据去问 B 的权限。
            # 两种都拒（fail-closed，铁律 #3）。返回 False 而非抛异常——check 的契约
            # 是给出布尔判定，异常留给 PEP 去翻译成 403。
            return False

        if _management_plane_denies(auth, action, context):
            return False

        if auth is not None:
            if auth.role is Role.ROOT:
                return True
            if actor == Scope():
                # 有认证上下文、role 又不是 ROOT：空 actor 只是一个**没填内容的
                # 身份**，不是特权形态。这里必须显式拒，否则它会命中下方
                # `_owner_scope_covers` 顶部的「parent 为空即覆盖一切」通配分支——
                # 那个分支是给 grant 行匹配用的，不该被 actor 借道。
                return False
        elif actor == Scope():
            # 无认证上下文时保留旧的 platform-admin 规则。有认证上下文时**不**保留：
            # 见下方 _management_plane_denies 上面的说明，特权必须来自认证层的显式
            # 结论，不能来自「actor 恰好是空 Scope」这个数据形状的巧合。
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


@PermissionProducer.register("sqlite")
def _build(config):
    db_path = config.get("db_path", ":memory:")
    return SQLitePermissionManager(str(db_path))
