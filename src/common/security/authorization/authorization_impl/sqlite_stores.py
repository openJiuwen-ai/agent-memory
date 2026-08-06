"""SQLite 后端的 :class:`GrantStore` 与 :class:`DelegationStore`。

从 ``control.permission_impl.sqlite_permission_manager`` 的 grants 表演进而来，
三处变化：

1. **一条记录一条授权**，不再按 action 拆行。旧表每个 action 一行，撤销要按
   (grantor, grantee, action) 十列匹配；有了 ``grant_id`` 之后按 id 撤销，而
   actions 存成排序后的逗号串。
2. **`revoked` 与 `expires_at` 在 SQL 里就过滤**（契约要求）。旧实现把过期条件写进
   了 WHERE，撤销也是；这里保持，但同时保留 ``revoked_at`` 时间戳供审计。
3. **新增 delegations 表**。旧实现没有委托真源——代操作靠 header 里的
   ``acting_user``，正是 F05 §从 header 直接产生 Delegation 拒绝的形态。

不做旧表迁移：grants 的列语义变了（多了 id、actions 合并成一列），且旧表是
``control`` 的所有物。PR3 删除旧 PermissionManager 时旧表随之废弃。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
    GrantStore,
    GrantStoreProducer,
)
from common.security.types import Action, Delegation, Grant
from common.type_def.scope import Scope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_grants (
    grant_id TEXT PRIMARY KEY,
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
    actions TEXT NOT NULL,
    expires_at TEXT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_grants_lookup
ON auth_grants (grantee_org, grantor_org, revoked_at);

CREATE TABLE IF NOT EXISTS auth_delegations (
    delegation_id TEXT PRIMARY KEY,
    delegator_org TEXT NOT NULL,
    delegator_space TEXT NOT NULL,
    delegator_user TEXT NOT NULL,
    delegator_agent TEXT NOT NULL,
    delegator_session TEXT NOT NULL,
    delegate_org TEXT NOT NULL,
    delegate_space TEXT NOT NULL,
    delegate_user TEXT NOT NULL,
    delegate_agent TEXT NOT NULL,
    delegate_session TEXT NOT NULL,
    actions TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    not_before TEXT NULL,
    allowed_spaces TEXT NOT NULL,
    bound_credential_id TEXT NOT NULL,
    bound_session TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT NULL
);
"""

_SCOPE_DIMENSIONS = ("org", "space", "user", "agent", "session")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    return None if not raw else datetime.fromisoformat(raw)


def _scope_values(scope: Scope) -> tuple[str, ...]:
    return tuple(getattr(scope, dim) for dim in _SCOPE_DIMENSIONS)


def _read_scope(row: sqlite3.Row, prefix: str) -> Scope:
    return Scope(**{dim: row[f"{prefix}_{dim}"] for dim in _SCOPE_DIMENSIONS})


def _dump_actions(actions: frozenset[Action]) -> str:
    """动作集合序列化成排序后的逗号串。

    排序是为了让同一集合有唯一表示——否则同样一条授权在两次写入后长得不一样，
    比对与去重都要先解析。
    """
    return ",".join(sorted(action.value for action in actions))


def _load_actions(raw: str) -> frozenset[Action]:
    """反序列化动作集合，**跳过**不认识的成员。

    库里出现核心不认识的动作名，只可能是降级部署（新版写入、旧版读取）或数据被改。
    两种情况都该按「这个动作不存在」处理——认不出就当没有，是 F05 §授权不变量 5
    「新 Action 默认拒绝」在存储层的对应形态。抛异常反而会让一条脏记录瘫掉整个
    授权查询。
    """
    result = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            result.add(Action(value))
        except ValueError:
            continue
    return frozenset(result)


class _SQLiteBacked:
    """两个 Store 共享的连接与建表逻辑。

    刻意**不是**公开基类：它只承载连接管理这一件与授权语义无关的事。两个 Store 的
    契约、Producer 和记录形态都各自独立。
    """

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def health(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SQLiteGrantStore(_SQLiteBacked, GrantStore):
    """SQLite 后端的授权表。"""

    def add(self, grant: Grant) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO auth_grants (
                    grant_id,
                    grantor_org, grantor_space, grantor_user,
                    grantor_agent, grantor_session,
                    grantee_org, grantee_space, grantee_user,
                    grantee_agent, grantee_session,
                    actions, expires_at, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grant_id) DO UPDATE SET
                    actions=excluded.actions,
                    expires_at=excluded.expires_at
                WHERE auth_grants.revoked_at IS NULL
                """,
                (
                    grant.grant_id,
                    *_scope_values(grant.grantor),
                    *_scope_values(grant.grantee),
                    _dump_actions(grant.actions),
                    _iso(grant.expires_at),
                    _iso(_now()),
                    _iso(_now()) if grant.revoked else None,
                ),
            )

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            # 只更新未撤销的行：重复撤销不该把撤销时间刷成第二次的，那会让审计里
            # 「权限何时消失」的答案随重试次数漂移。
            self._conn.execute(
                "UPDATE auth_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                (_iso(_now()), grant_id),
            )

    def find_active(
        self,
        *,
        grantee: Scope,
        grantor_org: str,
        action: Action,
        now: datetime,
    ) -> list[Grant]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM auth_grants
                WHERE revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND grantee_org=?
                  AND grantor_org=?
                """,
                (_iso(now), grantee.org, grantor_org),
            ).fetchall()
        # action 在 Python 侧筛：actions 是逗号串，SQL 的 LIKE '%read%' 会把
        # ``read_audit`` 当成 ``read`` 匹配上。按串匹配动作名是典型的子串陷阱。
        grants = []
        for row in rows:
            actions = _load_actions(row["actions"])
            if action not in actions:
                continue
            grants.append(
                Grant(
                    grant_id=row["grant_id"],
                    grantor=_read_scope(row, "grantor"),
                    grantee=_read_scope(row, "grantee"),
                    actions=actions,
                    expires_at=_parse_dt(row["expires_at"]),
                )
            )
        return grants


class SQLiteDelegationStore(_SQLiteBacked, DelegationStore):
    """SQLite 后端的委托表。"""

    def add(self, delegation: Delegation) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO auth_delegations (
                    delegation_id,
                    delegator_org, delegator_space, delegator_user,
                    delegator_agent, delegator_session,
                    delegate_org, delegate_space, delegate_user,
                    delegate_agent, delegate_session,
                    actions, expires_at, not_before, allowed_spaces,
                    bound_credential_id, bound_session, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delegation_id) DO UPDATE SET
                    actions=excluded.actions,
                    expires_at=excluded.expires_at,
                    not_before=excluded.not_before,
                    allowed_spaces=excluded.allowed_spaces
                WHERE auth_delegations.revoked_at IS NULL
                """,
                (
                    delegation.delegation_id,
                    *_scope_values(delegation.delegator),
                    *_scope_values(delegation.delegate),
                    _dump_actions(delegation.actions),
                    _iso(delegation.expires_at),
                    _iso(delegation.not_before),
                    ",".join(sorted(delegation.allowed_spaces)),
                    delegation.bound_credential_id,
                    delegation.bound_session,
                    _iso(_now()),
                    _iso(_now()) if delegation.revoked else None,
                ),
            )

    def revoke(self, delegation_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_delegations SET revoked_at=? "
                "WHERE delegation_id=? AND revoked_at IS NULL",
                (_iso(_now()), delegation_id),
            )

    def get(self, delegation_id: str) -> Delegation | None:
        if not delegation_id:
            # 空 id 不查表：``AuthContext.delegation_id`` 默认空串，让它命中一条
            # id 为空的记录等于给未声明委托的请求发了张通行证。
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM auth_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
        if row is None:
            return None
        spaces = frozenset(s for s in row["allowed_spaces"].split(",") if s)
        return Delegation(
            delegation_id=row["delegation_id"],
            delegator=_read_scope(row, "delegator"),
            delegate=_read_scope(row, "delegate"),
            actions=_load_actions(row["actions"]),
            # ``expires_at`` NOT NULL，故必然解析出值。
            expires_at=_parse_dt(row["expires_at"]),  # type: ignore[arg-type]
            not_before=_parse_dt(row["not_before"]),
            # 撤销状态**随记录一起返回**，由 Authorizer 用本次判定的同一个 now 复核
            # 时效。在这里判「有效与否」会让存储自己取一次 now，与 Grant 的时效判定
            # 错开。
            revoked=row["revoked_at"] is not None,
            allowed_spaces=spaces,
            bound_credential_id=row["bound_credential_id"],
            bound_session=row["bound_session"],
        )


@GrantStoreProducer.register("sqlite")
def _build_grant_store(config) -> SQLiteGrantStore:
    return SQLiteGrantStore(str(config.get("db_path", ":memory:")))


@DelegationStoreProducer.register("sqlite")
def _build_delegation_store(config) -> SQLiteDelegationStore:
    return SQLiteDelegationStore(str(config.get("db_path", ":memory:")))
