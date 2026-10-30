"""Test-owned vault inspection and public-contract privacy assertions."""

import copy
import json
import sqlite3
from pathlib import Path

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, RawPayload, Segment


class PrivacyState:
    """Open the configured test database independently of the provider's internals."""

    def __init__(self, path: Path):
        self.path = path
        self.connection = None

    def execute(self, sql: str, parameters=()):
        if self.connection is None:
            if not self.path.is_file():
                raise RuntimeError("privacy provider must create its state first")
            self.connection = sqlite3.connect(self.path, isolation_level=None)
        return self.connection.execute(sql, parameters)

    def close(self):
        if self.connection is not None:
            self.connection.close()

    def count(self, table: str, *, unit: str | None = None) -> int:
        """Inspect either legacy private SQL or the configured test-owned KV state."""
        allowed = {"fragments", "bindings", "manifests", "recovery_manifests"}
        if table not in allowed:
            raise ValueError("unknown private table")
        tables = {row[0] for row in self.execute("SELECT name FROM sqlite_master")}
        if "kv" not in tables:
            predicate = " WHERE unit=?" if unit is not None else ""
            params = (unit,) if unit is not None else ()
            return self.execute(f"SELECT COUNT(*) FROM {table}{predicate}", params).fetchone()[0]
        total = 0
        for (value,) in self.execute("SELECT value FROM kv WHERE key='/privacy/state/v1'"):
            data = json.loads(value)
            if table == "fragments":
                total += len(data["fragments"])
            elif table == "bindings":
                total += sum(
                    len(record["tokens"])
                    for name, record in data["active"].items()
                    if unit is None or name == unit
                )
            elif table == "manifests":
                total += len(data["active"])
            else:
                total += sum(len(records) for records in data["recovery"].values())
        return total

    def __enter__(self):
        return self

    @staticmethod
    def __exit__(*exc):
        return None


def assert_staging_unavailable(privacy, source: MemoryUnit | RawPayload) -> None:
    """A new unit with only a volatile source must not bind abandoned ciphertext."""
    if isinstance(source, RawPayload):
        source_ref, segments = source.id, [Segment(source.data.decode())]
    else:
        source_ref, segments = source.source_ref, copy.deepcopy(source.segments)
    probe = MemoryUnit(
        id="uncommitted-staging-probe",
        scope=source.scope,
        source_ref=source_ref,
        segments=segments,
    )
    try:
        privacy.persist(probe)
    except ValidationError as exc:
        if "unbound" not in str(exc):
            raise
        return
    raise AssertionError("abandoned volatile ciphertext still authorizes a new unit")
