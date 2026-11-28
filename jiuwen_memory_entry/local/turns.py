# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-turn storage for the local hooks (F17 D8): pending turns and task selection.

Keyed by ``{connector, project, host_session_id, turn_id}``: the same host session id in
two projects never interferes, and the connector is part of the key so two hosts on one
task cannot overwrite each other's turns.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_turn (
    connector        TEXT NOT NULL,
    project          TEXT NOT NULL,
    host_session_id  TEXT NOT NULL,
    turn_id          TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    binding_id       TEXT NOT NULL,
    user_text        TEXT NOT NULL,
    injected         TEXT NOT NULL DEFAULT '[]',
    expires_at       REAL NOT NULL,
    PRIMARY KEY (connector, project, host_session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS turn_operation (
    local_op_id      TEXT PRIMARY KEY,
    connector        TEXT NOT NULL,
    project          TEXT NOT NULL,
    host_session_id  TEXT NOT NULL,
    turn_id          TEXT NOT NULL,
    install_id       TEXT NOT NULL,
    actor            TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    binding_id       TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    digest           TEXT NOT NULL,
    status           TEXT NOT NULL,
    receipt          TEXT,
    request          TEXT,
    created_at       REAL NOT NULL,
    request_expires_at REAL NOT NULL,
    association_until REAL NOT NULL,
    UNIQUE (connector, project, host_session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS task_selection (
    connector        TEXT NOT NULL,
    project          TEXT NOT NULL,
    host_session_id  TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    binding_id       TEXT NOT NULL,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (connector, project, host_session_id)
);
"""


@dataclass(frozen=True)
class TurnKey:
    connector: str
    project: str
    host_session_id: str
    turn_id: str


@dataclass(frozen=True)
class PendingTurn:
    key: TurnKey
    task_id: str
    binding_id: str
    user_text: str
    expires_at: float
    injected: tuple[str, ...] = ()


@dataclass(frozen=True)
class Operation:
    """One frozen turn write (D8): association, status, receipt and original request."""

    local_op_id: str
    key: TurnKey
    install_id: str
    actor: dict
    task_id: str
    binding_id: str
    event_id: str
    digest: str
    status: str
    receipt: dict | None = None
    request: dict | None = None
    created_at: float = 0.0
    request_expires_at: float = 0.0
    association_until: float = 0.0


@dataclass(frozen=True)
class Selection:
    connector: str
    project: str
    host_session_id: str
    task_id: str
    binding_id: str


class TurnStore:
    """SQLite store for pending turns, selections and (later) operation records."""

    def __init__(self, db_path: str | Path, *, clock=time.time) -> None:
        path = str(db_path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        # Turns hold user prompts and answers verbatim: owner-only, even if the file
        # already existed with a looser mode.
        os.chmod(path, 0o600)
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def put_pending(
        self,
        key: TurnKey,
        task_id: str,
        binding_id: str,
        user_text: str,
        *,
        injected: Sequence[str] = (),
        ttl: float = 24 * 60 * 60,
    ) -> bool:
        """Atomically create this turn's pending record; an existing live record wins."""
        now = self._clock()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM pending_turn WHERE connector = ? AND project = ? "
                "AND host_session_id = ? AND turn_id = ? AND expires_at <= ?",
                (key.connector, key.project, key.host_session_id, key.turn_id, now),
            )
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO pending_turn "
                "(connector, project, host_session_id, turn_id, task_id, binding_id, "
                "user_text, injected, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key.connector,
                    key.project,
                    key.host_session_id,
                    key.turn_id,
                    task_id,
                    binding_id,
                    user_text,
                    json.dumps(list(injected), ensure_ascii=False),
                    now + ttl,
                ),
            )
            return cursor.rowcount > 0

    def purge_expired(self, now: float | None = None) -> int:
        """Actually delete expired pending rows; hiding them leaves the text on disk."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM pending_turn WHERE expires_at <= ?",
                (self._clock() if now is None else now,),
            )
            return cursor.rowcount

    def delete_pending(self, key: TurnKey) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM pending_turn WHERE connector = ? AND project = ? "
                "AND host_session_id = ? AND turn_id = ?",
                (key.connector, key.project, key.host_session_id, key.turn_id),
            )

    def get_pending(self, key: TurnKey) -> PendingTurn | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, binding_id, user_text, expires_at, injected FROM pending_turn "
                "WHERE connector = ? AND project = ? AND host_session_id = ? AND turn_id = ? "
                "AND expires_at > ?",
                (
                    key.connector,
                    key.project,
                    key.host_session_id,
                    key.turn_id,
                    self._clock(),
                ),
            ).fetchone()
        if row is None:
            return None
        return PendingTurn(
            key=key,
            task_id=row[0],
            binding_id=row[1],
            user_text=row[2],
            expires_at=row[3],
            injected=tuple(json.loads(row[4] or "[]")),
        )

    def set_selection(self, selection: Selection) -> None:
        now = self._clock()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO task_selection "
                "(connector, project, host_session_id, task_id, binding_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(connector, project, host_session_id) DO UPDATE SET "
                "task_id = excluded.task_id, binding_id = excluded.binding_id, "
                "updated_at = excluded.updated_at",
                (
                    selection.connector,
                    selection.project,
                    selection.host_session_id,
                    selection.task_id,
                    selection.binding_id,
                    now,
                ),
            )

    def get_selection(self, connector: str, project: str, host_session_id: str) -> Selection | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, binding_id FROM task_selection "
                "WHERE connector = ? AND project = ? AND host_session_id = ?",
                (connector, project, host_session_id),
            ).fetchone()
        if row is None:
            return None
        return Selection(connector, project, host_session_id, row[0], row[1])

    # -- operations ----------------------------------------------------------- #

    def create_operation(self, operation: Operation) -> Operation:
        """Insert exactly once per turn; a concurrent capture gets the existing record."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO turn_operation "
                "(local_op_id, connector, project, host_session_id, turn_id, install_id, "
                "actor, task_id, binding_id, event_id, digest, status, receipt, request, "
                "created_at, request_expires_at, association_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation.local_op_id,
                    operation.key.connector,
                    operation.key.project,
                    operation.key.host_session_id,
                    operation.key.turn_id,
                    operation.install_id,
                    json.dumps(operation.actor, ensure_ascii=False, sort_keys=True),
                    operation.task_id,
                    operation.binding_id,
                    operation.event_id,
                    operation.digest,
                    operation.status,
                    json.dumps(operation.receipt, ensure_ascii=False)
                    if operation.receipt is not None
                    else None,
                    json.dumps(operation.request, ensure_ascii=False)
                    if operation.request is not None
                    else None,
                    operation.created_at,
                    operation.request_expires_at,
                    operation.association_until,
                ),
            )
        existing = self.get_operation(operation.key)
        if existing is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError("operation record disappeared after insert")
        return existing

    def update_operation(self, local_op_id: str, **fields) -> Operation | None:
        allowed = {"status", "receipt", "request", "request_expires_at", "association_until"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown operation fields: {sorted(unknown)}")
        updates = []
        values: list[object] = []
        for name in sorted(fields):
            value = fields[name]
            if name in {"receipt", "request"}:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            updates.append(f"{name} = ?")
            values.append(value)
        if not updates:
            return self.get_operation_by_id(local_op_id)
        values.append(local_op_id)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE turn_operation SET {', '.join(updates)} WHERE local_op_id = ?", values
            )
        return self.get_operation_by_id(local_op_id)

    def get_operation(self, key: TurnKey) -> Operation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turn_operation WHERE connector = ? AND project = ? "
                "AND host_session_id = ? AND turn_id = ?",
                (key.connector, key.project, key.host_session_id, key.turn_id),
            ).fetchone()
        return self._operation_from_row(row)

    def list_operations(self, statuses: Sequence[str]) -> list[Operation]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM turn_operation WHERE status IN ({placeholders}) "
                "ORDER BY created_at",
                tuple(statuses),
            ).fetchall()
        operations = []
        for row in rows:
            operation = self._operation_from_row(row)
            if operation is not None:
                operations.append(operation)
        return operations

    def expire_requests(self, now: float | None = None) -> int:
        """Discard frozen requests past their retention; status and association stay."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE turn_operation SET request = NULL "
                "WHERE request IS NOT NULL AND request_expires_at <= ?",
                (self._clock() if now is None else now,),
            )
            return cursor.rowcount

    def get_operation_by_id(self, local_op_id: str) -> Operation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turn_operation WHERE local_op_id = ?", (local_op_id,)
            ).fetchone()
        return self._operation_from_row(row)

    @staticmethod
    def _operation_from_row(row) -> Operation | None:
        if row is None:
            return None
        (
            local_op_id, connector, project, host_session_id, turn_id, install_id, actor,
            task_id, binding_id, event_id, digest, status, receipt, request, created_at,
            request_expires_at, association_until,
        ) = row
        return Operation(
            local_op_id=local_op_id,
            key=TurnKey(connector, project, host_session_id, turn_id),
            install_id=install_id,
            actor=json.loads(actor),
            task_id=task_id,
            binding_id=binding_id,
            event_id=event_id,
            digest=digest,
            status=status,
            receipt=json.loads(receipt) if receipt else None,
            request=json.loads(request) if request else None,
            created_at=created_at,
            request_expires_at=request_expires_at,
            association_until=association_until,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
