"""Codex memories: stage1 SQLite facts, memory Markdown, opt-in rollouts."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jiuwen_memory_entry.local.sync.model import ImportItem, SourceFile
from jiuwen_memory_entry.local.sync.readers import _source, markdown_items


def codex_home() -> Path:
    return Path(os.environ.get("JIUWEN_CODEX_HOME", Path.home() / ".codex"))


class CodexReader:
    host = "codex"

    @staticmethod
    def default_root() -> Path | None:
        return codex_home()

    @staticmethod
    def discover(root: Path, *, sessions: bool) -> list[SourceFile]:
        files: list[SourceFile] = []
        db = root / "memories_1.sqlite"
        if db.is_file():
            files.append(_source(db, "fact"))
        for path in sorted((root / "memories").glob("*.md")):
            if path.is_file() and path.stat().st_size:
                files.append(_source(path, "fact"))
        if sessions:
            for path in sorted((root / "sessions").rglob("rollout-*.jsonl")):
                if path.is_file():
                    files.append(_source(path, "session_turn"))
        return files

    def read(
        self, source: SourceFile, *, since: datetime | None = None
    ) -> Iterable[ImportItem]:
        if source.path.suffix == ".sqlite":
            return self._facts(source)
        if source.path.suffix == ".jsonl":
            if since is not None and datetime.fromtimestamp(source.mtime) < since:
                return []
            return self._rollout(source)
        return markdown_items(
            source.path, source.path.parent, host=self.host, kind="fact"
        )

    @staticmethod
    def _facts(source: SourceFile) -> list[ImportItem]:
        """Legacy path; scans use the restricted SQLite connection instead."""
        del source
        return []

    @staticmethod
    def _rollout(source: SourceFile) -> list[ImportItem]:
        items: list[ImportItem] = []
        pending = ""
        for raw_line in source.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "response_item":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            texts = []
            for part in payload.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}:
                    texts.append(str(part.get("text", "")))
            text = "\n".join(texts).strip()
            if not text:
                continue
            if role == "user":
                pending = text
            elif pending:
                items.append(
                    ImportItem(
                        kind="session_turn",
                        text=f"user: {pending}\nassistant: {text}",
                        source_key=f"codex:{source.path.name}#turn:{len(items)}",
                        locator=f"{source.path.name}#{len(items) + 1}",
                    )
                )
                pending = ""
        return items


    def scan(self, access, *, types, since, limit):
        return _scan_codex(self, access, types=types, since=since, limit=limit)


def _scan_codex(self, access, *, types, since, limit):
    from jiuwen_memory_entry.local.sync.model import ItemId, RangeKey, ScanResult
    from jiuwen_memory_entry.local.sync.readers import scan_markdown

    result = ScanResult()
    count = 0
    for root in access.roots:
        db = root / "memories_1.sqlite"
        if db.is_file() and "fact" in types:
            key = RangeKey(str(root), "fact")
            result.complete.setdefault(key, True)
            try:
                conn = access.open_sqlite(db, kind="fact")
                try:
                    rows = conn.execute(
                        "SELECT thread_id, source_updated_at, raw_memory FROM stage1_outputs"
                    ).fetchall()
                finally:
                    conn.close()
            except Exception:
                result.complete[key] = False
                result.skipped.append((str(db), "sqlite busy or unsupported"))
                rows = []
            for thread_id, updated, raw in rows:
                if limit is not None and count >= limit:
                    result.complete[key] = False
                    break
                text = str(raw or "").strip()
                if not text:
                    continue
                when = None
                try:
                    when = datetime.fromisoformat(str(updated)) if updated else None
                except ValueError:
                    when = None
                if since is not None and when is not None and when < since:
                    result.complete[key] = False
                    continue
                result.items.append(
                    ImportItem(
                        item_id=ItemId("fact", "codex:stage1_outputs", str(thread_id)),
                        text=text, occurred_at=when, locator="stage1_outputs",
                        root=str(root),
                    )
                )
                count += 1
        for path in sorted((root / "memories").glob("*.md")):
            key = RangeKey(str(root), "fact")
            result.complete.setdefault(key, True)
            if "fact" not in types:
                continue
            items = scan_markdown(
                access, path, host="codex", kind="fact",
                collection=path.relative_to(root).as_posix(),
            )
            for item in items:
                if limit is not None and count >= limit:
                    result.complete[key] = False
                    break
                result.items.append(item)
                count += 1
        if "session_turn" in types:
            for path in sorted((root / "sessions").rglob("rollout-*.jsonl")):
                key = RangeKey(str(root), "session_turn")
                result.complete.setdefault(key, True)
                if since is not None and datetime.fromtimestamp(path.stat().st_mtime) < since:
                    result.complete[key] = False
                    continue
                if limit is not None and count >= limit:
                    result.complete[key] = False
                    continue
                for item in _rollout_from_text(
                    access.open_text(path, kind="session_turn"), path.name,
                    root=str(root),
                ):
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


def _rollout_from_text(text, name, root=""):
    from jiuwen_memory_entry.local.sync.model import ItemId
    items = []
    pending = ""
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        texts = [
            str(part.get("text", ""))
            for part in payload.get("content") or []
            if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
        ]
        content = "\n".join(texts).strip()
        if not content:
            continue
        if role == "user":
            pending = content
        elif pending:
            items.append(
                ImportItem(
                    item_id=ItemId("session_turn", name, f"turn:{len(items)}"),
                    text=f"user: {pending}\nassistant: {content}",
                    locator=f"{name}#{len(items) + 1}",
                    root=root,
                )
            )
            pending = ""
    return items
