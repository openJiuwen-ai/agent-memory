"""WorkBuddy memory files (USER.md and dated daily logs)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jiuwen_memory_entry.local.hosts.workbuddy import detect_workbuddy
from jiuwen_memory_entry.local.sync.model import ImportItem, SourceFile
from jiuwen_memory_entry.local.sync.readers import _source, markdown_items

EXCLUDED = {"SOUL.md", "IDENTITY.md", "AGENTS.md", "models.json", "mcp.json"}


class WorkBuddyReader:
    host = "workbuddy"

    @staticmethod
    def default_root() -> Path | None:
        layout = detect_workbuddy()
        return layout.home if layout else None

    @staticmethod
    def discover(root: Path, *, sessions: bool) -> list[SourceFile]:
        files: list[SourceFile] = []
        user = root / "USER.md"
        if user.is_file() and user.name not in EXCLUDED and user.stat().st_size:
            files.append(_source(user, "profile"))
        for directory in (
            root / ".workbuddy" / "memory",
            root / "memory",
            root / "memories",
        ):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.md")):
                if path.name in EXCLUDED or not path.stat().st_size:
                    continue
                files.append(_source(path, "daily_log"))
        return files

    def read(
        self, source: SourceFile, *, since: datetime | None = None
    ) -> Iterable[ImportItem]:
        kind = "profile" if source.path.name == "USER.md" else "daily_log"
        occurred = None
        match = re.search(r"(\d{4}-\d{2}-\d{2})", source.path.name)
        if match:
            try:
                occurred = datetime.fromisoformat(match.group(1))
            except ValueError:
                occurred = None
        return markdown_items(
            source.path,
            source.path.parent,
            host=self.host,
            kind=kind,
            occurred_at=occurred,
        )


    def scan(self, access, *, types, since, limit):
        return _scan_workbuddy(self, access, types=types, since=since, limit=limit)


def _scan_workbuddy(self, access, *, types, since, limit):
    from jiuwen_memory_entry.local.sync.model import ItemId, RangeKey, ScanResult
    from jiuwen_memory_entry.local.sync.readers import scan_markdown

    result = ScanResult()
    count = 0
    for root in access.roots:
        user = root / "USER.md"
        if user.is_file():
            key = RangeKey(str(root), "profile")
            result.complete.setdefault(key, True)
            if "profile" not in types:
                result.complete[key] = False
                result.skipped.append((str(user), "type not authorized"))
            elif since is not None and datetime.fromtimestamp(user.stat().st_mtime) < since:
                result.complete[key] = False
            elif limit is None or count < limit:
                result.items.append(
                    ImportItem(
                        item_id=ItemId(
                            "profile", user.relative_to(root).as_posix(), "file"
                        ),
                        text=access.open_text(user, kind="profile"),
                        locator="USER.md",
                        root=str(root),
                    )
                )
                count += 1
        for directory in (
            root / ".workbuddy" / "memory",
            root / "memory",
            root / "memories",
        ):
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.md")):
                if path.name in EXCLUDED or not path.stat().st_size:
                    continue
                key = RangeKey(str(root), "daily_log")
                result.complete.setdefault(key, True)
                if "daily_log" not in types:
                    result.complete[key] = False
                    result.skipped.append((str(path), "type not authorized"))
                    continue
                if since is not None and datetime.fromtimestamp(path.stat().st_mtime) < since:
                    result.complete[key] = False
                    continue
                if limit is not None and count >= limit:
                    result.complete[key] = False
                    continue
                relative = path.relative_to(root).as_posix()
                items = scan_markdown(
                    access, path, host="workbuddy", kind="daily_log",
                    collection=relative,
                )
                for item in items:
                    if limit is not None and count >= limit:
                        result.complete[key] = False
                        break
                    result.items.append(item)
                    count += 1
    if access.truncated:
        for key in list(result.complete):
            result.complete[key] = False
    result.skipped.extend(access.skipped)
    result.skipped.extend((path, "file exceeds 2 MiB") for path in access.truncated)
    return result
