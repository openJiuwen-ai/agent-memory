"""Hermes memory files: MEMORY.md facts and USER.md profile entries."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jiuwen_memory_entry.local.sync.model import ImportItem, SourceFile
from jiuwen_memory_entry.local.sync.readers import _source, markdown_items


class HermesReader:
    host = "hermes"

    @staticmethod
    def default_root() -> Path | None:
        return Path(os.environ.get("JIUWEN_HERMES_HOME", Path.home() / ".hermes"))

    @staticmethod
    def discover(root: Path, *, sessions: bool) -> list[SourceFile]:
        files: list[SourceFile] = []
        for directory in [root / "memories", *sorted((root / "profiles").glob("*/memories"))]:
            if not directory.is_dir():
                continue
            for name, kind in (("MEMORY.md", "fact"), ("USER.md", "profile")):
                path = directory / name
                if path.is_file() and path.stat().st_size:
                    files.append(_source(path, kind))
        if sessions:
            db = root / "state.db"
            if db.is_file():
                files.append(_source(db, "session_turn"))
        return files

    def read(
        self, source: SourceFile, *, since: datetime | None = None
    ) -> Iterable[ImportItem]:
        if source.path.name == "state.db":
            return self._sessions(source, since)
        kind = "profile" if source.path.name == "USER.md" else "fact"
        return markdown_items(
            source.path,
            self.default_root() or source.path.parent,
            host=self.host,
            kind=kind,
        )

    @staticmethod
    def _sessions(source: SourceFile, since: datetime | None) -> list[ImportItem]:
        """Legacy path; scans use the restricted SQLite connection instead."""
        del source, since
        return []


    def scan(self, access, *, types, since, limit):
        return _scan_hermes(self, access, types=types, since=since, limit=limit)


def _scan_hermes(self, access, *, types, since, limit):
    from jiuwen_memory_entry.local.sync.model import ItemId, RangeKey, ScanResult
    from jiuwen_memory_entry.local.sync.readers import scan_markdown

    result = ScanResult()
    count = 0
    for root in access.roots:
        directories = [root / "memories", *sorted((root / "profiles").glob("*/memories"))]
        for directory in directories:
            profile = directory.parent.name if "profiles" in directory.parts else "default"
            for name, kind in (("MEMORY.md", "fact"), ("USER.md", "profile")):
                path = directory / name
                if not path.is_file():
                    continue
                key = RangeKey(str(root), kind)
                result.complete.setdefault(key, True)
                if kind not in types:
                    result.complete[key] = False
                    result.skipped.append((str(path), "type not authorized"))
                    continue
                if since is not None and datetime.fromtimestamp(path.stat().st_mtime) < since:
                    result.complete[key] = False
                    continue
                if limit is not None and count >= limit:
                    result.complete[key] = False
                    continue
                items = scan_markdown(
                    access,
                    path,
                    host="hermes",
                    kind=kind,
                    collection=f"{profile}:{'MEMORY' if kind == 'fact' else 'USER'}",
                )
                for item in items:
                    if limit is not None and count >= limit:
                        result.complete[key] = False
                        break
                    result.items.append(item)
                    count += 1
        if "session_turn" in types:
            db = root / "state.db"
            key = RangeKey(str(root), "session_turn")
            if db.is_file():
                result.complete.setdefault(key, True)
                if since is not None or limit is not None:
                    result.complete[key] = False
                try:
                    conn = access.open_sqlite(db, kind="session_turn")
                    try:
                        rows = conn.execute(
                            "SELECT rowid, role, content, created_at FROM messages "
                            "WHERE role IN ('user','assistant') ORDER BY created_at"
                        ).fetchall()
                    finally:
                        conn.close()
                except Exception as exc:  # noqa: BLE001
                    result.complete[key] = False
                    result.skipped.append((str(db), type(exc).__name__))
                    rows = []
                pending = ""
                pending_id = ""
                for rowid, role, content, created in rows:
                    text = str(content or "").strip()
                    if not text:
                        continue
                    when = datetime.fromtimestamp(float(created)) if created else None
                    if since is not None and when is not None and when < since:
                        continue
                    if role == "user":
                        pending = text
                        pending_id = str(rowid)
                    elif pending:
                        if limit is not None and count >= limit:
                            result.complete[key] = False
                            break
                        result.items.append(
                            ImportItem(
                                item_id=ItemId(
                                    "session_turn", "hermes:state.db",
                                    f"{pending_id}:{rowid}",
                                ),
                                text=f"user: {pending}\nassistant: {text}",
                                occurred_at=when,
                                locator=f"state.db#{rowid}",
                                root=str(root),
                            )
                        )
                        count += 1
                        pending = ""
                        pending_id = ""
        if access.truncated:
            for key in list(result.complete):
                result.complete[key] = False
    result.skipped.extend(access.skipped)
    result.skipped.extend((path, "file exceeds 2 MiB") for path in access.truncated)
    return result
