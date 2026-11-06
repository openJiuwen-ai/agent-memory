# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SQLite private-state backend. Encryption belongs to the wrapping Store."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from jiuwen_memory.common.errors import BackendError, ConflictError, NotFoundError
from jiuwen_memory.common.security._sqlite_state import open_private_state
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.privacy import (
    FragmentReference,
    PrivacySnapshot,
    PrivacyStore,
    PrivacyStoreProducer,
)
from jiuwen_memory.storage.privacy import (
    scope_key as scope_key,
)


class SQLitePrivacyStore(PrivacyStore):
    """Transactional snapshots, fragments and recovery bindings in a dedicated file."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        try:
            self._db = open_private_state(
                path,
                """
                PRAGMA foreign_keys=ON;
                PRAGMA secure_delete=ON;
                CREATE TABLE IF NOT EXISTS fragments(
                    scope TEXT NOT NULL, token TEXT NOT NULL, cipher BLOB NOT NULL,
                    level TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY(scope,token));
                CREATE TABLE IF NOT EXISTS manifests(
                    scope TEXT NOT NULL, unit TEXT NOT NULL, revision TEXT NOT NULL,
                    digest TEXT NOT NULL, cipher BLOB NOT NULL, PRIMARY KEY(scope,unit));
                CREATE TABLE IF NOT EXISTS bindings(
                    scope TEXT NOT NULL, unit TEXT NOT NULL, token TEXT NOT NULL,
                    PRIMARY KEY(scope,unit,token),
                    FOREIGN KEY(scope,unit) REFERENCES manifests(scope,unit) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS bindings_token ON bindings(scope,token);
                CREATE TABLE IF NOT EXISTS recovery_manifests(
                    scope TEXT NOT NULL, unit TEXT NOT NULL, revision TEXT NOT NULL,
                    digest TEXT NOT NULL, cipher BLOB NOT NULL, PRIMARY KEY(scope,unit,revision));
                CREATE TABLE IF NOT EXISTS recovery_bindings(
                    scope TEXT NOT NULL, unit TEXT NOT NULL,
                    revision TEXT NOT NULL, token TEXT NOT NULL,
                    PRIMARY KEY(scope,unit,revision,token),
                    FOREIGN KEY(scope,unit,revision)
                        REFERENCES recovery_manifests(scope,unit,revision) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS recovery_token ON recovery_bindings(scope,token);
                """,
            )
        except sqlite3.Error as exc:
            raise BackendError("privacy storage initialization failed") from exc

    @contextmanager
    def transaction(self, scope: Scope):
        with self._lock:
            name = f"privacy_{self._depth}"
            self._depth += 1
            try:
                self._db.execute(f"SAVEPOINT {name}")
                try:
                    yield
                except BaseException:
                    self._db.execute(f"ROLLBACK TO {name}")
                    self._db.execute(f"RELEASE {name}")
                    raise
                else:
                    self._db.execute(f"RELEASE {name}")
            except sqlite3.Error as exc:
                raise BackendError("privacy storage operation failed") from exc
            finally:
                self._depth -= 1

    @staticmethod
    def stage_fragment(scope: Scope, token: str, value: bytes, level: str) -> FragmentReference:
        return FragmentReference(value, level, scope_key(scope))

    def reference(self, scope: Scope, token: str) -> FragmentReference:
        with self.transaction(scope):
            row = self._db.execute(
                "SELECT cipher,level,origin FROM fragments WHERE scope=? AND token=?",
                (scope_key(scope), token),
            ).fetchone()
            if row is None:
                raise NotFoundError("privacy fragment is unavailable")
            return FragmentReference(*row)

    def get_fragment(self, scope: Scope, token: str) -> bytes:
        return self.reference(scope, token).value

    def get(self, scope: Scope, unit: str) -> PrivacySnapshot:
        with self.transaction(scope):
            row = self._db.execute(
                "SELECT unit,revision,digest,cipher FROM manifests WHERE scope=? AND unit=?",
                (scope_key(scope), unit),
            ).fetchone()
            if row is None:
                raise NotFoundError("privacy snapshot is unavailable")
            return PrivacySnapshot(*row)

    def search(
        self, scope: Scope, units: list[str], *, revision: str | None = None
    ) -> list[PrivacySnapshot]:
        with self.transaction(scope):
            rows = []
            # Point lookups avoid SQLite's parameter limit for large provenance lists.
            for unit in dict.fromkeys(units):
                for table in ("manifests", "recovery_manifests"):
                    sql = (
                        f"SELECT unit,revision,digest,cipher FROM {table} WHERE scope=? AND unit=?"
                    )
                    params = (scope_key(scope), unit)
                    if revision is not None:
                        sql += " AND revision=?"
                        params += (revision,)
                    rows.extend(self._db.execute(sql, params).fetchall())
            return [PrivacySnapshot(*row) for row in rows]

    def insert(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        with self.transaction(scope):
            try:
                self.get(scope, snapshot.unit)
            except NotFoundError:
                self._replace(scope, snapshot, fragments)
            else:
                raise ConflictError("privacy snapshot already exists")

    def update(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        with self.transaction(scope):
            self.get(scope, snapshot.unit)
            self._replace(scope, snapshot, fragments)

    def _replace(
        self, scope: Scope, snapshot: PrivacySnapshot, fragments: dict[str, FragmentReference]
    ) -> None:
        namespace = scope_key(scope)
        self._db.executemany(
            "INSERT OR IGNORE INTO fragments VALUES(?,?,?,?,?)",
            [
                (namespace, token, ref.value, ref.level, ref.origin)
                for token, ref in fragments.items()
            ],
        )
        self._db.execute(
            "DELETE FROM manifests WHERE scope=? AND unit=?", (namespace, snapshot.unit)
        )
        self._db.execute(
            "INSERT INTO manifests VALUES(?,?,?,?,?)",
            (namespace, snapshot.unit, snapshot.revision, snapshot.digest, snapshot.value),
        )
        self._db.executemany(
            "INSERT INTO bindings VALUES(?,?,?)",
            [(namespace, snapshot.unit, token) for token in fragments],
        )
        self._collect()

    def checkpoint(self, scope: Scope, unit: str) -> None:
        with self.transaction(scope):
            namespace = scope_key(scope)
            self._db.execute(
                "INSERT OR IGNORE INTO recovery_manifests "
                "SELECT * FROM manifests WHERE scope=? AND unit=?",
                (namespace, unit),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO recovery_bindings "
                "SELECT b.scope,b.unit,m.revision,b.token FROM bindings b "
                "JOIN manifests m ON b.scope=m.scope AND b.unit=m.unit "
                "WHERE b.scope=? AND b.unit=?",
                (namespace, unit),
            )

    def confirm(self, scope: Scope, unit: str) -> None:
        with self.transaction(scope):
            self._db.execute(
                "DELETE FROM recovery_manifests WHERE scope=? AND unit=?", (scope_key(scope), unit)
            )
            self._collect()

    def reconcile(self, scope: Scope, unit: str, revision: str, digest: str) -> None:
        with self.transaction(scope):
            namespace = scope_key(scope)
            active = self._db.execute(
                "SELECT revision,digest FROM manifests WHERE scope=? AND unit=?", (namespace, unit)
            ).fetchone()
            if active != (revision, digest):
                recovered = self._db.execute(
                    "SELECT * FROM recovery_manifests "
                    "WHERE scope=? AND unit=? AND revision=? AND digest=?",
                    (namespace, unit, revision, digest),
                ).fetchone()
                if recovered is None:
                    return
                self._db.execute(
                    "DELETE FROM manifests WHERE scope=? AND unit=?", (namespace, unit)
                )
                self._db.execute("INSERT INTO manifests VALUES(?,?,?,?,?)", recovered)
                self._db.execute(
                    "INSERT INTO bindings SELECT scope,unit,token FROM recovery_bindings "
                    "WHERE scope=? AND unit=? AND revision=?",
                    (namespace, unit, revision),
                )
            self.confirm(scope, unit)

    def delete(self, scope: Scope, units: list[str] | None) -> set[str]:
        with self.transaction(scope):
            namespace = scope_key(scope)
            tokens = set()
            if units is None:
                rows = self._db.execute(
                    "SELECT unit FROM manifests WHERE scope=? "
                    "UNION SELECT unit FROM recovery_manifests WHERE scope=?",
                    (namespace, namespace),
                )
                units = [row[0] for row in rows]
            for unit in units:
                rows = self._db.execute(
                    "SELECT token FROM bindings WHERE scope=? AND unit=? "
                    "UNION SELECT token FROM recovery_bindings WHERE scope=? AND unit=?",
                    (namespace, unit) * 2,
                )
                tokens.update(row[0] for row in rows)
                self._db.execute(
                    "DELETE FROM manifests WHERE scope=? AND unit=?", (namespace, unit)
                )
                self._db.execute(
                    "DELETE FROM recovery_manifests WHERE scope=? AND unit=?", (namespace, unit)
                )
            self._collect()
            return tokens

    def _collect(self) -> None:
        self._db.execute(
            "DELETE FROM fragments WHERE NOT EXISTS (SELECT 1 FROM bindings b "
            "WHERE b.scope=fragments.scope AND b.token=fragments.token) "
            "AND NOT EXISTS (SELECT 1 FROM recovery_bindings b "
            "WHERE b.scope=fragments.scope AND b.token=fragments.token)"
        )

    def scopes(self) -> list[Scope]:
        with self.transaction(Scope()):
            rows = self._db.execute(
                "SELECT scope FROM manifests UNION SELECT scope FROM recovery_manifests"
            ).fetchall()
            return [Scope(**json.loads(row[0])) for row in rows]

    def health(self) -> None:
        with self.transaction(Scope()):
            self._db.execute("SELECT 1")

    def close(self) -> None:
        with self._lock:
            self._db.close()


@PrivacyStoreProducer.register("sqlite")
def _build(config):
    from jiuwen_memory.common.errors import ValidationError

    path = config.get("db_path")
    if not isinstance(path, str) or not path:
        raise ValidationError("privacy_store.sqlite requires db_path")
    return SQLitePrivacyStore(path)
