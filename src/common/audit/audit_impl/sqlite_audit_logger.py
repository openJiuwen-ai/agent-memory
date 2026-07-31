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

            # 读取当前 user_version（在检查表之前，审计 P1-2）
            current_version = self._conn.execute("PRAGMA user_version").fetchone()[0]

            # 检查表是否存在（审计 P1-2：区分表被 DROP 重建 vs 表一直存在）
            table_existed = (
                self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_chain_head'"
                ).fetchone()
                is not None
            )

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_chain_head (
                    id INTEGER PRIMARY KEY CHECK (id = 0),
                    head_hmac TEXT NOT NULL DEFAULT '',
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    schema_version INTEGER NOT NULL DEFAULT 2
                )
                """
            )

            # 如果表刚刚被创建（之前不存在），检查是全新库还是表被 DROP（审计 P1-2）
            if not table_existed:
                event_count = self._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

                if event_count > 0 and current_version >= 2:
                    # 有事件 + user_version >= 2 = 当前库 DROP 表（损坏，审计 P1-2）
                    # 严格规则：版本 2 的任何 head schema 缺失都是损坏，不降级！
                    # 保持 version >= 2，让恢复逻辑按当前 schema 缺 head 拒绝
                    pass
                elif event_count == 0 and current_version == 0:
                    # 全新空库：表刚创建、无事件、user_version=0，设置为当前版本
                    self._conn.execute("PRAGMA user_version = 2")
                    current_version = 2
                elif event_count > 0 and current_version == 0:
                    # 有事件 + user_version=0 = 真正的旧库（从未有过 head 表）
                    # 不设置版本，等待列迁移逻辑处理
                    pass

            # 旧库迁移（审计 P1-1/P2-1）：表已存在但缺列
            # 严格规则（审计 P1-2）：只有 user_version < 2 才允许迁移，>= 2 则拒绝
            head_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(audit_chain_head)").fetchall()
            }
            migration_needed = False

            if current_version < 2:
                # 只有旧版才允许添加列
                if "last_seq" not in head_cols:
                    self._conn.execute(
                        "ALTER TABLE audit_chain_head ADD COLUMN last_seq "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                    migration_needed = True
                if "schema_version" not in head_cols:
                    # 缺 schema_version = 旧版表迁移而来（审计 P2-1：区分迁移态 vs 篡改态）
                    self._conn.execute(
                        "ALTER TABLE audit_chain_head ADD COLUMN schema_version "
                        "INTEGER NOT NULL DEFAULT 1"
                    )
                    migration_needed = True

                # 如果执行了列迁移，设置 user_version=1（审计 P1-2）
                if migration_needed:
                    self._conn.execute("PRAGMA user_version = 1")
                elif current_version == 0:
                    # 表结构完整但 user_version=0：旧库场景（表被 DROP 重建），标记为迁移态
                    self._conn.execute("PRAGMA user_version = 1")
            else:
                # current_version >= 2：当前版本，不允许任何列迁移
                # 如果列不完整，保持 version >= 2，让恢复逻辑拒绝（审计 P1-2）
                pass
            # 不在此插空行--head 初始化由 HmacAuditLogger._recover_chain_head 负责。

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
        """初始化/迁移 chain-head（审计 P1-1/P2-1）：旧库升级时由 HmacAuditLogger 调用。

        验证旧链后，把 head 设为最后一条事件的 _hmac + seq，并升级 schema_version=2
        （标记迁移完成，审计 P2-1）。同时设置 PRAGMA user_version=2（审计 P1-2：
        独立版本元数据，不可被行删除破坏）。INSERT OR REPLACE 覆盖整行。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_chain_head"
                " (id, head_hmac, last_seq, schema_version) VALUES (0, ?, ?, 2)",
                (hmac, last_seq),
            )
            # 迁移完成：升级 user_version 到 2（审计 P1-2）
            self._conn.execute("PRAGMA user_version = 2")

    def get_last_event(self) -> AuditEvent | None:
        """最后一条事件（O(1)，供启动一致性校验比对 head，审计 P1-2）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return _row_to_event(row) if row else None

    def get_chain_state(self) -> tuple[str, int, int, str, int]:
        """跨连接一致的链状态快照（审计 P1）：单条 SQL 同时读 head + last_event。

        返回 ``(head_hmac, head_last_seq, last_event_seq, last_event_hmac, schema_version)``。
        SQLite 单条 statement 自带一致快照，不受其他连接的并发写入影响。

        schema_version 从 PRAGMA user_version 读取（审计 P1-2）：独立元数据，不受
        head 行删除影响。``< 2`` 表示旧版迁移态（允许回填 last_seq），审计 P2-1。

        使用 singleton CTE 锚点确保查询永远返回一行（审计 P1-1），避免 head 行不存在时
        退回两次查询导致并发窗口。

        当前 schema 表结构不完整时（审计 P1-2），用 db_version >= 2 标识损坏，恢复逻辑
        会拒绝启动。
        """
        import json as _json

        with self._lock:
            # 读取 user_version（独立元数据，不受行删除影响，审计 P1-2）
            db_version = self._conn.execute("PRAGMA user_version").fetchone()[0]

            # 检查列是否存在（审计 P1-2：当前库表结构不完整时保护性读取）
            head_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(audit_chain_head)").fetchall()
            }
            has_last_seq = "last_seq" in head_cols
            has_schema_version = "schema_version" in head_cols

            # 构建动态 SQL：只读取存在的列
            if has_last_seq and has_schema_version:
                # 完整结构
                select_clause = """
                    h.head_hmac AS head_hmac,
                    h.last_seq AS head_last_seq,
                    e.seq AS last_event_seq,
                    e.detail_json AS last_event_detail,
                    h.schema_version AS schema_version
                """
            else:
                # 不完整结构（当前库损坏，审计 P1-2）
                select_clause = """
                    h.head_hmac AS head_hmac,
                    0 AS head_last_seq,
                    e.seq AS last_event_seq,
                    e.detail_json AS last_event_detail,
                    1 AS schema_version
                """

            # 单 SQL 永远返回一行（审计 P1-1）：用 singleton CTE 锚点 LEFT JOIN
            row = self._conn.execute(
                f"""
                WITH anchor AS (SELECT 1 AS placeholder)
                SELECT {select_clause}
                FROM anchor
                LEFT JOIN audit_chain_head h ON h.id = 0
                LEFT JOIN audit_events e ON e.seq = (SELECT MAX(seq) FROM audit_events)
                """
            ).fetchone()

        # row 永远非 None，但列可能为 NULL
        head_hmac = row["head_hmac"] or ""
        head_last_seq = row["head_last_seq"] or 0
        last_seq = row["last_event_seq"] or 0
        last_hmac = ""
        if row["last_event_detail"]:
            detail = _json.loads(row["last_event_detail"])
            last_hmac = detail.get("_hmac", "")
        # 优先使用 db_version（防止表列被篡改）
        schema_version = db_version if db_version > 0 else (row["schema_version"] or 1)
        return (head_hmac, head_last_seq, last_seq, last_hmac, schema_version)

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
