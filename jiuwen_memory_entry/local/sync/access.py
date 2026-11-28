"""Restricted, read-only access handed to sync readers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jiuwen_memory_entry.local.sync.grant import SyncGrant

MAX_READ_BYTES = 2 * 1024 * 1024


class AccessDenied(PermissionError):
    pass


class SourceAccess:
    def __init__(self, grant: SyncGrant) -> None:
        self.grant = grant
        self.roots = tuple(Path(item).expanduser().resolve() for item in grant.roots)
        self.opened: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.truncated: list[str] = []

    def _resolve(self, relative: str | Path) -> Path:
        candidate = Path(relative)
        if not candidate.is_absolute():
            candidates = [root / candidate for root in self.roots]
            candidate = next((item for item in candidates if item.exists()), candidates[0])
        resolved = candidate.resolve()
        if not any(
            resolved == root or root in resolved.parents for root in self.roots
        ):
            raise AccessDenied(f"path escapes sync roots: {relative}")
        return resolved

    def iterdir(self, relative: str | Path) -> list[Path]:
        try:
            directory = self._resolve(relative)
        except AccessDenied as exc:
            self.skipped.append((str(relative), str(exc)))
            return []
        if not directory.is_dir():
            return []
        return sorted(directory.iterdir())

    def open_bytes(self, relative: str | Path, *, kind: str) -> bytes:
        path = self._authorized(relative, kind)
        data = path.read_bytes()
        if len(data) > MAX_READ_BYTES:
            self.truncated.append(str(path))
        self.opened.append(str(path))
        return data[:MAX_READ_BYTES]

    def _authorized(self, relative: str | Path, kind: str) -> Path:
        if kind not in self.grant.types:
            self.skipped.append((str(relative), f"type {kind!r} is not authorized"))
            raise AccessDenied(f"type {kind!r} is not authorized")
        try:
            return self._resolve(relative)
        except AccessDenied as exc:
            self.skipped.append((str(relative), str(exc)))
            raise

    def open_text(self, relative: str | Path, *, kind: str) -> str:
        path = self._authorized(relative, kind)
        data = path.read_bytes()
        if len(data) > MAX_READ_BYTES:
            self.truncated.append(str(path))
        self.opened.append(str(path))
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace").lstrip("\ufeff")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def open_sqlite(self, relative: str | Path, *, kind: str) -> sqlite3.Connection:
        path = self._authorized(relative, kind)
        self.opened.append(str(path))
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        return conn
