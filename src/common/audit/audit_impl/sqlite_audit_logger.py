"""SQLite-backed audit logger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Iterable, NamedTuple

from common.audit.base import AuditLogger, AuditProducer
from common.type_def import AuditEvent, Scope


class AuditRow(NamedTuple):
    id: str
    actor_org: str
    actor_space: str
    actor_user: str
    actor_agent: str
    actor_session: str
    target_org: str
    target_space: str
    target_user: str
    target_agent: str
    target_session: str
    action: str
    target_id: str
    layer: str
    decision: str
    occurred_at: str | None
    detail_json: str


class SqliteAuditLogger(AuditLogger):
    """Persist audit events in a local SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL,
                    actor_org TEXT NOT NULL,
                    actor_space TEXT NOT NULL,
                    actor_user TEXT NOT NULL,
                    actor_agent TEXT NOT NULL,
                    actor_session TEXT NOT NULL,
                    target_org TEXT NOT NULL,
                    target_space TEXT NOT NULL,
                    target_user TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    target_session TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    occurred_at TEXT,
                    detail_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_action_layer "
                "ON audit_events(action, layer, seq)"
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(audit_events)").fetchall()
            }
            if "actor_space" not in columns:
                self._conn.execute(
                    "ALTER TABLE audit_events ADD COLUMN actor_space TEXT NOT NULL DEFAULT ''"
                )
            for column in (
                "target_org",
                "target_space",
                "target_user",
                "target_agent",
                "target_session",
            ):
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE audit_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            # chain-head 表（审计 PR③ P1-1）：单行存当前链头 HMAC + last_seq，供事务 CAS。
            # 多实例/多连接写同一库时，BEGIN IMMEDIATE + head(seq+HMAC)比较-并-交换保证链
            # 不分叉。last_seq 同时用于启动一致性校验（head 必须与最后事件一致，审计 P1-2）。
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_chain_head (
                    id INTEGER PRIMARY KEY CHECK (id = 0),
                    head_hmac TEXT NOT NULL DEFAULT '',
                    last_seq INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 旧库迁移（审计 P1-1）：表已存在但缺 last_seq 列（上一版 schema）-> 加列。
            head_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(audit_chain_head)").fetchall()
            }
            if "last_seq" not in head_cols:
                self._conn.execute(
                    "ALTER TABLE audit_chain_head ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0"
                )
            # 不在此插空行--head 初始化由 HmacAuditLogger._recover_chain_head 负责
            # （它有 key 能验证旧链后才填，避免旧签名库 head 被空初始化断链，审计 P1-1）。

    def record_chained(self, event: AuditEvent, expected_head: str) -> str:
        """事务 CAS 追加链式事件（审计 PR③ P1-1）。

        ``BEGIN IMMEDIATE`` 下：读 chain_head(seq+hmac)、验证 hmac == expected_head
        （否则 ``ConflictError`` 重试）、插 event、更新 head 为新 seq+hmac，同一事务提交。
        多实例/多连接写同一库时串行化，保证链头读取-验证-追加-提交原子。

        返回新 head HMAC。调用方据此推进内存链头。
        """
        from common.errors import ConflictError

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT head_hmac, last_seq FROM audit_chain_head WHERE id = 0"
                ).fetchone()
                current = row["head_hmac"] if row else ""
                if current != expected_head:
                    self._conn.execute("ROLLBACK")
                    raise ConflictError(
                        "audit chain head changed: 另一实例已写入，请重读 head 重试"
                    )
                self._conn.execute(*self._insert_sql(event))
                new_seq = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                new_head = event.detail.get("_hmac", "")
                # INSERT OR REPLACE：首条写入时行不存在（_init_schema 不插空行，避免
                # 旧库 head 被空初始化），后续写入覆盖。保证 head 表总有行。
                self._conn.execute(
                    "INSERT OR REPLACE INTO audit_chain_head"
                    " (id, head_hmac, last_seq) VALUES (0, ?, ?)",
                    (new_head, new_seq),
                )
                self._conn.execute("COMMIT")
            except ConflictError:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return new_head

    def get_chain_head(self) -> str:
        """读当前链头 HMAC（O(1)，供恢复与 CAS 重试）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT head_hmac FROM audit_chain_head WHERE id = 0"
            ).fetchone()
            return row["head_hmac"] if row else ""

    def init_chain_head(self, hmac: str, last_seq: int) -> None:
        """初始化/迁移 chain-head（审计 P1-1）：旧库升级时由 HmacAuditLogger 调用。

        验证旧链后，把 head 设为最后一条事件的 _hmac + seq。INSERT OR REPLACE
        覆盖空行（_init_schema 不插空行，首条 record_chained 或迁移在此初始化）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_chain_head"
                " (id, head_hmac, last_seq) VALUES (0, ?, ?)",
                (hmac, last_seq),
            )

    def get_last_event(self) -> AuditEvent | None:
        """最后一条事件（O(1)，供启动一致性校验比对 head，审计 P1-2）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return _row_to_event(row) if row else None

    def get_chain_state(self) -> tuple[str, int, int, str]:
        """在同一锁内返回 ``(head_hmac, head_last_seq, last_event_seq, last_event_hmac)``。

        稳定快照（审计 P1-2b）：避免 ``get_chain_head`` 与 ``get_last_event`` 两次独立
        读取之间被并发追加插入不一致状态。调用方据此比对 head 与最后事件是否一致。
        """
        with self._lock:
            head_row = self._conn.execute(
                "SELECT head_hmac, last_seq FROM audit_chain_head WHERE id = 0"
            ).fetchone()
            last_row = self._conn.execute(
                "SELECT seq, detail_json FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        head_hmac = head_row["head_hmac"] if head_row else ""
        head_last_seq = head_row["last_seq"] if head_row else 0
        last_seq = last_row["seq"] if last_row else 0
        last_hmac = ""
        if last_row:
            import json as _json

            detail = _json.loads(last_row["detail_json"])
            last_hmac = detail.get("_hmac", "")
        return (head_hmac, head_last_seq, last_seq, last_hmac)

    def _insert_sql(self, event: AuditEvent):
        """单条 INSERT 的 SQL + params（record_chained 复用）。"""
        row = _event_to_row(event)
        return (
            """
            INSERT INTO audit_events (
                id, actor_org, actor_space, actor_user, actor_agent, actor_session,
                target_org, target_space, target_user, target_agent, target_session,
                action, target_id, layer, decision, occurred_at, detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row),
        )

    def record(self, event: AuditEvent) -> None:
        self._record_many([event])

    def _record_many(self, events: Iterable[AuditEvent]) -> None:
        """Internal bulk insert hook for a future public record_many API."""
        rows = [_event_to_row(event) for event in events]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO audit_events (
                    id, actor_org, actor_space, actor_user, actor_agent, actor_session,
                    target_org, target_space, target_user, target_agent, target_session,
                    action, target_id, layer, decision, occurred_at, detail_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def query(
        self, filters: dict[str, str], limit: int = 100, *, offset: int = 0
    ) -> list[AuditEvent]:
        with self._lock:
            clauses: list[str] = []
            params: list[object] = []
            exact_fields = {
                "action": "action",
                "layer": "layer",
                "decision": "decision",
                "target_id": "target_id",
                "actor_org": "actor_org",
                "actor_space": "actor_space",
                "actor_user": "actor_user",
                "actor_agent": "actor_agent",
                "actor_session": "actor_session",
                "target_org": "target_org",
                "target_space": "target_space",
                "target_user": "target_user",
                "target_agent": "target_agent",
                "target_session": "target_session",
            }
            for field, column in exact_fields.items():
                if filters.get(field):
                    clauses.append(f"{column} = ?")
                    params.append(filters[field])
            if filters.get("occurred_after"):
                clauses.append("datetime(occurred_at) >= datetime(?)")
                params.append(filters["occurred_after"])
            if filters.get("occurred_before"):
                clauses.append("datetime(occurred_at) <= datetime(?)")
                params.append(filters["occurred_before"])
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            params.append(offset)
            rows = self._conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY seq ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def tail(self, limit: int = 1) -> list[AuditEvent]:
        """最近 ``limit`` 条事件（O(1)：``ORDER BY seq DESC LIMIT ?`` 再反序）。

        覆盖默认的全量实现，供链式 HMAC 恢复链头--避免每次启动全表读（审计 P2-1）。
        DESC 取再反序，使返回仍是写入序（最早在前），与 query 语义一致。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def iter_chain(self, after_seq: int = 0, limit: int = 1000) -> list[tuple[int, AuditEvent]]:
        """keyset 分页（审计 P2-1）：``WHERE seq > ? ORDER BY seq LIMIT ?``。

        覆盖默认的 OFFSET 实现--OFFSET 跳过前置行是 O(n)，全量验证趋近二次复杂度。
        keyset 用上一页最后 seq 作下一页 after_seq，O(limit)。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (after_seq, limit),
            ).fetchall()
        return [(row["seq"], _row_to_event(row)) for row in rows]


@AuditProducer.register("sqlite")
def _build(config):
    return SqliteAuditLogger(config.get("db_path", ":memory:"))


def _event_to_row(event: AuditEvent) -> AuditRow:
    occurred_at = event.occurred_at.isoformat() if event.occurred_at else None
    detail_json = json.dumps(event.detail, ensure_ascii=False, sort_keys=True)
    return AuditRow(
        id=event.id,
        actor_org=event.actor.org,
        actor_space=event.actor.space,
        actor_user=event.actor.user,
        actor_agent=event.actor.agent,
        actor_session=event.actor.session,
        target_org=event.target.org,
        target_space=event.target.space,
        target_user=event.target.user,
        target_agent=event.target.agent,
        target_session=event.target.session,
        action=event.action,
        target_id=event.target_id,
        layer=event.layer,
        decision=event.decision,
        occurred_at=occurred_at,
        detail_json=detail_json,
    )


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    occurred_at = row["occurred_at"]
    return AuditEvent(
        id=row["id"],
        actor=Scope(
            org=row["actor_org"],
            space=row["actor_space"],
            user=row["actor_user"],
            agent=row["actor_agent"],
            session=row["actor_session"],
        ),
        target=Scope(
            org=row["target_org"],
            space=row["target_space"],
            user=row["target_user"],
            agent=row["target_agent"],
            session=row["target_session"],
        ),
        action=row["action"],
        target_id=row["target_id"],
        layer=row["layer"],
        decision=row["decision"],
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
        detail=json.loads(row["detail_json"]),
    )
