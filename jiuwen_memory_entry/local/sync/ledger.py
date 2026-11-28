"""SQLite sync ledger: one row per install/source key."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple

_TABLES = """
CREATE TABLE IF NOT EXISTS sync_source (
    install_id TEXT NOT NULL, root TEXT NOT NULL, item_id TEXT NOT NULL,
    target_space TEXT NOT NULL, digest TEXT NOT NULL, op_id TEXT NOT NULL,
    intent TEXT NOT NULL, old_refs TEXT NOT NULL, refs TEXT NOT NULL,
    pending_refs TEXT NOT NULL, grant_id TEXT NOT NULL,
    grant_version INTEGER NOT NULL, state TEXT NOT NULL, last_seen REAL NOT NULL,
    PRIMARY KEY (install_id, root, item_id, target_space)
);
CREATE TABLE IF NOT EXISTS sync_output (
    unit_id TEXT NOT NULL PRIMARY KEY, target_space TEXT NOT NULL, origin TEXT NOT NULL,
    created_by_op TEXT NOT NULL, initial_digest TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0, refcount INTEGER NOT NULL DEFAULT 0,
    retained_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_pending (
    unit_id TEXT NOT NULL, target_space TEXT NOT NULL, source_key TEXT NOT NULL,
    reason TEXT NOT NULL, blocked_by_op TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (unit_id, target_space, source_key)
);
CREATE TABLE IF NOT EXISTS sync_ref (
    source_key TEXT NOT NULL, unit_id TEXT NOT NULL,
    PRIMARY KEY (source_key, unit_id)
);
"""

_TABLE = """
CREATE TABLE IF NOT EXISTS sync_ledger (
    install_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    unit_ids TEXT NOT NULL,
    digest TEXT NOT NULL,
    seen_at REAL NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (install_id, source_key)
)
"""


@dataclass(frozen=True)
class LedgerRow:
    install_id: str
    source_key: str
    unit_ids: list[str]
    digest: str
    seen_at: float
    state: Literal["ok", "pending"]


class SyncLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_TABLES)
            self._conn.execute(_TABLE)
            self._backfill_refs_locked()
            self._conn.execute(
                "UPDATE sync_output SET protected=0, retained_reason='' "
                "WHERE protected=1 AND retained_reason='conditional delete unsupported'"
            )

    def _backfill_refs_locked(self) -> None:
        # Older ledgers tracked references only as a refcount; rebuild the exact
        # source->unit relation once so release/recovery is idempotent.
        if self._conn.execute("SELECT COUNT(*) FROM sync_ref").fetchone()[0]:
            return
        if not self._conn.execute(
            "SELECT COUNT(*) FROM sync_output WHERE refcount > 0"
        ).fetchone()[0]:
            # Every reference was released (possibly just before close); do not
            # resurrect it from the legacy source row on the next open.
            return
        rows = self._conn.execute(
            "SELECT install_id, root, item_id, target_space, grant_id, refs "
            "FROM sync_source"
        ).fetchall()
        for install_id, root, item_id, space, grant_id, refs_json in rows:
            source_key = f"{grant_id}|{space}|{root}|{item_id}"
            for unit_id in json.loads(refs_json):
                self._conn.execute(
                    "INSERT OR IGNORE INTO sync_ref(source_key, unit_id) VALUES (?,?)",
                    (source_key, unit_id),
                )
        self._conn.execute(
            "UPDATE sync_output SET refcount=("
            "SELECT COUNT(*) FROM sync_ref WHERE sync_ref.unit_id=sync_output.unit_id)"
            " WHERE unit_id IN (SELECT unit_id FROM sync_ref)"
        )

    def get_source_by_key(self, install_id: str, source_key: str) -> SourceRow | None:
        parts = source_key.split("|", 3)
        if len(parts) != 4:
            return None
        grant_id, target_space, root, item_id = parts
        source = self.get_source(install_id, root, item_id, target_space)
        if source is None or source.grant_id != grant_id:
            return None
        return source

    def get(self, install_id: str, source_key: str) -> LedgerRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT install_id, source_key, unit_ids, digest, seen_at, state "
                "FROM sync_ledger WHERE install_id = ? AND source_key = ?",
                (install_id, source_key),
            ).fetchone()
        if row is None:
            return None
        return LedgerRow(
            install_id=row[0], source_key=row[1], unit_ids=json.loads(row[2]),
            digest=row[3], seen_at=float(row[4]), state=row[5],
        )

    def upsert(
        self, install_id: str, source_key: str, unit_ids: list[str], digest: str,
        seen_at: float, state: Literal["ok", "pending"] = "ok",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_ledger "
                "(install_id, source_key, unit_ids, digest, seen_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(install_id, source_key) DO UPDATE SET "
                "unit_ids = excluded.unit_ids, digest = excluded.digest, "
                "seen_at = excluded.seen_at, state = excluded.state",
                (install_id, source_key, json.dumps(unit_ids), digest, seen_at, state),
            )

    def touch(self, install_id: str, source_key: str, seen_at: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sync_ledger SET seen_at = ? WHERE install_id = ? AND source_key = ?",
                (seen_at, install_id, source_key),
            )

    def unseen(self, install_id: str, since: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_key FROM sync_ledger WHERE install_id = ? AND seen_at < ?",
                (install_id, since),
            ).fetchall()
        return [row[0] for row in rows]

    def delete(self, install_id: str, source_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM sync_ledger WHERE install_id = ? AND source_key = ?",
                (install_id, source_key),
            )

    def delete_source(
        self, install_id: str, root: str, item_id: str, target_space: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM sync_source WHERE install_id=? AND root=? AND item_id=? "
                "AND target_space=?",
                (install_id, root, item_id, target_space),
            )

    def put_source(self, row: SourceRow) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_source VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(install_id, root, item_id, target_space) DO UPDATE SET "
                "digest=excluded.digest, op_id=excluded.op_id, intent=excluded.intent, "
                "old_refs=excluded.old_refs, refs=excluded.refs, "
                "pending_refs=excluded.pending_refs, grant_id=excluded.grant_id, "
                "grant_version=excluded.grant_version, state=excluded.state, "
                "last_seen=excluded.last_seen",
                _source_columns(row),
            )

    def get_source(
        self, install_id: str, root: str, item_id: str, target_space: str
    ) -> SourceRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT install_id, root, item_id, target_space, digest, op_id, intent, "
                "old_refs, refs, pending_refs, grant_id, grant_version, state, last_seen "
                "FROM sync_source WHERE install_id=? AND root=? AND item_id=? "
                "AND target_space=?",
                (install_id, root, item_id, target_space),
            ).fetchone()
        if row is None:
            return None
        return SourceRow(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6],
            json.loads(row[7]), json.loads(row[8]), json.loads(row[9]),
            row[10], int(row[11]), row[12], float(row[13]),
        )

    def mark_retained(self, unit_ids: list[str], reason: str) -> None:
        with self._lock, self._conn:
            for unit_id in unit_ids:
                self._conn.execute(
                    "UPDATE sync_output SET retained_reason=? WHERE unit_id=?",
                    (reason, unit_id),
                )

    def record_recovered(
        self,
        row: SourceRow,
        refs: list[str],
        digest: str,
        seen_at: float,
        *,
        old_key: str = "",
        release_old: bool = True,
        pending_reason: str = "",
        pending_op: str = "",
    ) -> None:
        """Atomically update references, source row and scan row after a recovery."""
        source_key = f"{row.grant_id}|{row.target_space}|{row.root}|{row.item_id}"
        old_key = old_key or source_key
        new_refs = set(refs)
        released: list[str] = []
        with self._lock, self._conn:
            for unit_id in row.old_refs if release_old else ():
                if old_key == source_key and unit_id in new_refs:
                    continue
                cursor = self._conn.execute(
                    "DELETE FROM sync_ref WHERE source_key=? AND unit_id=?",
                    (old_key, unit_id),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        "UPDATE sync_output SET refcount=MAX(0, refcount-1) "
                        "WHERE unit_id=?",
                        (unit_id,),
                    )
                self._conn.execute(
                    "DELETE FROM sync_pending WHERE unit_id=? AND source_key=?",
                    (unit_id, old_key),
                )
                released.append(unit_id)
            if pending_reason:
                for unit_id in released:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO sync_pending VALUES (?,?,?,?,?,?)",
                        (
                            unit_id,
                            row.target_space,
                            old_key,
                            pending_reason,
                            pending_op,
                            seen_at,
                        ),
                    )
            for unit_id in refs:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO sync_ref(source_key, unit_id) VALUES (?,?)",
                    (source_key, unit_id),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        "UPDATE sync_output SET refcount=refcount+1 WHERE unit_id=?",
                        (unit_id,),
                    )
            self._conn.execute(
                "INSERT INTO sync_ledger "
                "(install_id, source_key, unit_ids, digest, seen_at, state) "
                "VALUES (?, ?, ?, ?, ?, 'ok') ON CONFLICT(install_id, source_key) "
                "DO UPDATE SET unit_ids=excluded.unit_ids, digest=excluded.digest, "
                "seen_at=excluded.seen_at, state='ok'",
                (row.install_id, source_key, json.dumps(refs), digest, seen_at),
            )
            self._conn.execute(
                "INSERT INTO sync_source VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(install_id, root, item_id, target_space) DO UPDATE SET "
                "digest=excluded.digest, op_id=excluded.op_id, intent=excluded.intent, "
                "old_refs=excluded.old_refs, refs=excluded.refs, "
                "pending_refs=excluded.pending_refs, grant_id=excluded.grant_id, "
                "grant_version=excluded.grant_version, state=excluded.state, "
                "last_seen=excluded.last_seen",
                _source_columns(row),
            )

    def put_output(self, output: OutputRow) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_output VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(unit_id) DO UPDATE SET target_space=excluded.target_space, "
                "created_by_op=excluded.created_by_op, "
                "initial_digest=excluded.initial_digest, protected=excluded.protected, "
                "retained_reason=excluded.retained_reason",
                (
                    output.unit_id, output.target_space, output.origin,
                    output.created_by_op, output.initial_digest,
                    int(output.protected), output.refcount, output.retained_reason,
                ),
            )

    def get_output(self, unit_id: str) -> OutputRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT unit_id, target_space, origin, created_by_op, initial_digest, "
                "protected, refcount, retained_reason FROM sync_output WHERE unit_id=?",
                (unit_id,),
            ).fetchone()
        if row is None:
            return None
        return OutputRow(
            row[0], row[1], row[2], row[3], row[4], bool(row[5]), int(row[6]), row[7]
        )

    def add_refs(self, source_key, unit_ids: list[str]) -> None:
        key = _source_key(source_key)
        with self._lock, self._conn:
            for unit_id in unit_ids:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO sync_ref(source_key, unit_id) VALUES (?,?)",
                    (key, unit_id),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        "UPDATE sync_output SET refcount=refcount+1 WHERE unit_id=?",
                        (unit_id,),
                    )

    def release_refs(self, source_key, unit_ids: list[str]) -> list[str]:
        key = _source_key(source_key)
        zeroed: list[str] = []
        with self._lock, self._conn:
            for unit_id in unit_ids:
                cursor = self._conn.execute(
                    "DELETE FROM sync_ref WHERE source_key=? AND unit_id=?", (key, unit_id)
                )
                if cursor.rowcount:
                    self._conn.execute(
                        "UPDATE sync_output SET refcount=MAX(0, refcount-1) "
                        "WHERE unit_id=?",
                        (unit_id,),
                    )
                self._conn.execute(
                    "DELETE FROM sync_pending WHERE unit_id=? AND source_key=?",
                    (unit_id, key),
                )
                row = self._conn.execute(
                    "SELECT refcount FROM sync_output WHERE unit_id=?", (unit_id,)
                ).fetchone()
                if row is not None and int(row[0]) == 0:
                    zeroed.append(unit_id)
        return zeroed

    def protect(self, unit_ids: list[str], reason: str) -> None:
        with self._lock, self._conn:
            for unit_id in unit_ids:
                self._conn.execute(
                    "UPDATE sync_output SET protected=1, retained_reason=? "
                    "WHERE unit_id=?",
                    (reason, unit_id),
                )

    def pending_for_space(self, space: str) -> list[PendingCleanup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT unit_id, target_space, source_key, reason, blocked_by_op, "
                "created_at FROM sync_pending WHERE target_space=? OR target_space=''",
                (space,),
            ).fetchall()
        return [PendingCleanup(*row) for row in rows]

    def pending_cleanups(self, space: str) -> list[PendingCleanup]:
        return self.pending_for_space(space)

    def unconfirmed_ops(self, space: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT op_id FROM sync_source WHERE target_space=? "
                "AND state IN ('writing','unconfirmed')",
                (space,),
            ).fetchall()
        return [row[0] for row in rows]


    def sources_by_op(self, op_id: str) -> list[SourceRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT install_id, root, item_id, target_space, digest, op_id, intent, "
                "old_refs, refs, pending_refs, grant_id, grant_version, state, last_seen "
                "FROM sync_source WHERE op_id=?",
                (op_id,),
            ).fetchall()
        return [
            SourceRow(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                json.loads(row[7]), json.loads(row[8]), json.loads(row[9]),
                row[10], int(row[11]), row[12], float(row[13]),
            )
            for row in rows
        ]

    def outputs_by_op(self, op_id: str) -> list[OutputRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT unit_id, target_space, origin, created_by_op, initial_digest, "
                "protected, refcount, retained_reason FROM sync_output "
                "WHERE created_by_op=?",
                (op_id,),
            ).fetchall()
        return [
            OutputRow(
                row[0], row[1], row[2], row[3], row[4], bool(row[5]),
                int(row[6]), row[7],
            )
            for row in rows
        ]

    def sources_for_install(self, install_id: str) -> list[SourceRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT install_id, root, item_id, target_space, digest, op_id, intent, "
                "old_refs, refs, pending_refs, grant_id, grant_version, state, last_seen "
                "FROM sync_source WHERE install_id=?",
                (install_id,),
            ).fetchall()
        return [
            SourceRow(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                json.loads(row[7]), json.loads(row[8]), json.loads(row[9]),
                row[10], int(row[11]), row[12], float(row[13]),
            )
            for row in rows
        ]

    def sources_for_space(self, space: str) -> list[SourceRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT install_id, root, item_id, target_space, digest, op_id, intent, "
                "old_refs, refs, pending_refs, grant_id, grant_version, state, last_seen "
                "FROM sync_source WHERE target_space=?",
                (space,),
            ).fetchall()
        return [
            SourceRow(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                json.loads(row[7]), json.loads(row[8]), json.loads(row[9]),
                row[10], int(row[11]), row[12], float(row[13]),
            )
            for row in rows
        ]

    def sources_for_grant(self, grant_id: str) -> list[SourceRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT install_id, root, item_id, target_space, digest, op_id, intent, "
                "old_refs, refs, pending_refs, grant_id, grant_version, state, last_seen "
                "FROM sync_source WHERE grant_id=?",
                (grant_id,),
            ).fetchall()
        return [
            SourceRow(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                json.loads(row[7]), json.loads(row[8]), json.loads(row[9]),
                row[10], int(row[11]), row[12], float(row[13]),
            )
            for row in rows
        ]

    def put_pending(self, cleanup: PendingCleanup) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_pending VALUES (?,?,?,?,?,?)",
                (
                    cleanup.unit_id, cleanup.target_space, cleanup.source_key,
                    cleanup.reason, cleanup.blocked_by_op, cleanup.created_at,
                ),
            )

    def delete_pending(self, unit_id: str, target_space: str, source_key: str = "") -> None:
        with self._lock, self._conn:
            if source_key:
                self._conn.execute(
                    "DELETE FROM sync_pending WHERE unit_id=? AND target_space=? "
                    "AND source_key=?",
                    (unit_id, target_space, source_key),
                )
            else:
                self._conn.execute(
                    "DELETE FROM sync_pending WHERE unit_id=? AND target_space=?",
                    (unit_id, target_space),
                )

    def update_source_state(self, row: SourceRow, state: str) -> None:
        updated = SourceRow(
            row.install_id, row.root, row.item_id, row.target_space, row.digest,
            row.op_id, row.intent, list(row.old_refs), list(row.refs),
            list(row.pending_refs), row.grant_id, row.grant_version, state,
            row.last_seen,
        )
        self.put_source(updated)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass
class SourceRow:
    install_id: str
    root: str
    item_id: str
    target_space: str
    digest: str
    op_id: str = ""
    intent: str = "new"
    old_refs: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    pending_refs: list[str] = field(default_factory=list)
    grant_id: str = ""
    grant_version: int = 1
    state: str = "prepared"
    last_seen: float = 0.0

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.install_id, self.root, self.item_id, self.target_space)


@dataclass
class OutputRow:
    unit_id: str
    target_space: str
    origin: str
    created_by_op: str
    initial_digest: str
    protected: bool = False
    refcount: int = 0
    retained_reason: str = ""


@dataclass
class PendingCleanup:
    unit_id: str
    target_space: str
    source_key: str
    reason: str
    blocked_by_op: str = ""
    created_at: float = 0.0


def can_delete(output: OutputRow, unit_metadata: dict[str, str]) -> tuple[bool, str]:
    if output.origin != "sync":
        return False, "origin is not sync"
    if unit_metadata.get("sync_created_by") != output.created_by_op:
        return False, "sync_created_by does not match"
    if output.protected:
        return False, "unit is protected"
    if output.refcount != 0:
        return False, "unit still has references"
    return True, ""


def _source_key(source_key) -> str:
    if isinstance(source_key, (tuple, list)):
        return "|".join(str(item) for item in source_key)
    return str(source_key)


class _SourceColumns(NamedTuple):
    install_id: str
    root: str
    item_id: str
    target_space: str
    digest: str
    op_id: str
    intent: str
    old_refs: str
    refs: str
    pending_refs: str
    grant_id: str
    grant_version: int
    state: str
    last_seen: float


def _source_columns(row: SourceRow) -> _SourceColumns:
    return _SourceColumns(
        row.install_id, row.root, row.item_id, row.target_space, row.digest,
        row.op_id, row.intent, json.dumps(row.old_refs), json.dumps(row.refs),
        json.dumps(row.pending_refs), row.grant_id, row.grant_version, row.state,
        row.last_seen,
    )
