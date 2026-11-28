"""DSH sessions: only known v4 event streams, decompressed read-only."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jiuwen_memory_entry.local.sync.model import ImportItem, SourceFile
from jiuwen_memory_entry.local.sync.readers import _source

_VERSION = re.compile(r"session\.v(\d+)\.jsonl\.zstd$")


def dsh_home() -> Path:
    return Path(os.environ.get("JIUWEN_DSH_HOME", Path.home() / ".dsh"))


def _text(event: dict) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    for key in ("text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("text") or value.get("content")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        if isinstance(value, list):
            parts = [
                str(item.get("text", "")) for item in value
                if isinstance(item, dict) and item.get("text")
            ]
            if parts:
                return "\n".join(parts).strip()
    return ""


class DshReader:
    host = "dsh"

    @staticmethod
    def default_root() -> Path | None:
        return dsh_home()

    @staticmethod
    def discover(root: Path, *, sessions: bool) -> list[SourceFile]:
        if not sessions:
            return []
        return [
            _source(path, "session_turn")
            for path in sorted((root / "sessions").glob("*/session-*/session.v*.jsonl.zstd"))
            if path.is_file()
        ]

    @staticmethod
    def read(source: SourceFile, *, since: datetime | None = None
    ) -> Iterable[ImportItem]:
        match = _VERSION.search(source.path.name)
        if match is None or match.group(1) != "4":
            version = match.group(1) if match else "unknown"
            raise ValueError(f"unsupported DSH session format v{version}")
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError("需要安装 JiuwenMemory[local] (zstandard)") from exc
        raw = zstandard.ZstdDecompressor().decompress(
            source.path.read_bytes(), max_output_size=16 * 1024 * 1024
        )
        items: list[ImportItem] = []
        collection = source.path.as_posix()
        user = ""
        assistant = ""
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type", ""))
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            if kind == "user/message":
                source_value = str(
                    payload.get("source") or event.get("source") or ""
                ).lower()
                if source_value not in {"user", "human", "external"}:
                    continue
                user = _text(event)
            elif kind in {"assistant/message", "assistant/final", "assistant"}:
                assistant = _text(event)
            elif kind == "turn/end":
                if user and assistant:
                    items.append(
                        ImportItem(
                            kind="session_turn",
                            text=f"user: {user}\nassistant: {assistant}",
                            source_key=f"dsh:{collection}#turn:{len(items)}",
                            locator=f"{collection}#{len(items) + 1}",
                        )
                    )
                user = ""
                assistant = ""
        if user and assistant:
            items.append(
                ImportItem(
                    kind="session_turn",
                    text=f"user: {user}\nassistant: {assistant}",
                    source_key=f"dsh:{collection}#turn:{len(items)}",
                    locator=f"{collection}#{len(items) + 1}",
                )
            )
        return items


    def scan(self, access, *, types, since, limit):
        return _scan_dsh(self, access, types=types, since=since, limit=limit)


def _scan_dsh(self, access, *, types, since, limit):
    from jiuwen_memory_entry.local.sync.model import ItemId, RangeKey, ScanResult

    result = ScanResult()
    if "session_turn" not in types:
        return result
    count = 0
    for root in access.roots:
        for path in sorted((root / "sessions").glob("*/session-*/session.v*.jsonl.zstd")):
            key = RangeKey(str(root), "session_turn")
            result.complete.setdefault(key, True)
            if since is not None and datetime.fromtimestamp(path.stat().st_mtime) < since:
                result.complete[key] = False
                continue
            if limit is not None and count >= limit:
                result.complete[key] = False
                continue
            match = _VERSION.search(path.name)
            if match is None or match.group(1) != "4":
                result.complete[key] = False
                result.skipped.append((str(path), "unsupported DSH session version"))
                continue
            try:
                import zstandard
            except ImportError:
                result.complete[key] = False
                result.skipped.append((str(path), "zstandard is not installed"))
                continue
            try:
                raw = zstandard.ZstdDecompressor().decompress(
                    access.open_bytes(path, kind="session_turn"),
                    max_output_size=16 * 1024 * 1024,
                )
            except Exception:
                result.complete[key] = False
                result.skipped.append((str(path), "cannot decompress"))
                continue
            # Identity must come from the file itself: a global counter renumbers later
            # sessions when an earlier one changes, which rewrites and deletes the
            # wrong turns. ``collection`` keeps same-named files in other session
            # directories apart.
            collection = path.relative_to(root).as_posix()
            turn = 0
            user = ""
            assistant = ""
            for line in raw.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type", ""))
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                if kind == "user/message":
                    source_value = str(
                        payload.get("source") or event.get("source") or ""
                    ).lower()
                    if source_value in {"user", "human", "external"}:
                        user = _text(event)
                elif kind in {"assistant/message", "assistant/final", "assistant"}:
                    assistant = _text(event)
                elif kind == "turn/end" and user and assistant:
                    if limit is not None and count >= limit:
                        result.complete[key] = False
                        break
                    result.items.append(
                        ImportItem(
                            item_id=ItemId("session_turn", collection, f"turn:{turn}"),
                            text=f"user: {user}\nassistant: {assistant}",
                            locator=f"{collection}#{turn + 1}",
                            root=str(root),
                        )
                    )
                    turn += 1
                    count += 1
                    user = assistant = ""
            turn_ready = bool(user and assistant)
            within_limit = limit is None or count < limit
            if turn_ready and within_limit:
                result.items.append(
                    ImportItem(
                        item_id=ItemId("session_turn", collection, f"turn:{turn}"),
                        text=f"user: {user}\nassistant: {assistant}",
                        locator=f"{collection}#{turn + 1}",
                        root=str(root),
                    )
                )
                count += 1
    if access.truncated:
        for key in list(result.complete):
            result.complete[key] = False
    result.skipped.extend(access.skipped)
    result.skipped.extend((path, "file exceeds 2 MiB") for path in access.truncated)
    return result
