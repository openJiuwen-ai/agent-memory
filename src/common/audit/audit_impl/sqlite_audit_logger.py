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
    actor_user: str
    actor_agent: str
    actor_session: str
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
                    actor_user TEXT NOT NULL,
                    actor_agent TEXT NOT NULL,
                    actor_session TEXT NOT NULL,
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
                    id, actor_org, actor_user, actor_agent, actor_session,
                    action, target_id, layer, decision, occurred_at, detail_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        with self._lock:
            clauses: list[str] = []
            params: list[object] = []
            exact_fields = {
                "action": "action",
                "layer": "layer",
                "decision": "decision",
                "target_id": "target_id",
                "actor_org": "actor_org",
                "actor_user": "actor_user",
                "actor_agent": "actor_agent",
                "actor_session": "actor_session",
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
            rows = self._conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY seq ASC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]


@AuditProducer.register("sqlite")
def _build(config):
    return SqliteAuditLogger(config.get("db_path", ":memory:"))


def _event_to_row(event: AuditEvent) -> AuditRow:
    occurred_at = event.occurred_at.isoformat() if event.occurred_at else None
    detail_json = json.dumps(event.detail, ensure_ascii=False, sort_keys=True)
    return AuditRow(
        id=event.id,
        actor_org=event.actor.org,
        actor_user=event.actor.user,
        actor_agent=event.actor.agent,
        actor_session=event.actor.session,
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
            user=row["actor_user"],
            agent=row["actor_agent"],
            session=row["actor_session"],
        ),
        action=row["action"],
        target_id=row["target_id"],
        layer=row["layer"],
        decision=row["decision"],
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
        detail=json.loads(row["detail_json"]),
    )
