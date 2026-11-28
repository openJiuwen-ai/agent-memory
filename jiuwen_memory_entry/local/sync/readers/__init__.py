"""Read-only host memory readers. Host differences stay in these plugins."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from jiuwen_memory_entry.local.sync.model import (
    ImportItem,
    ItemId,
    ScanResult,
    SourceFile,
)

MAX_READ_BYTES = 2 * 1024 * 1024


class SourceReader(Protocol):
    host: str

    def default_root(self) -> Path | None:
        ...

    def discover(self, root: Path, *, sessions: bool) -> list[SourceFile]:
        ...

    def read(
        self, source: SourceFile, *, since: datetime | None = None
    ) -> Iterable[ImportItem]:
        ...

    def scan(
        self,
        access,
        *,
        types: frozenset[str],
        since: datetime | None,
        limit: int | None,
    ) -> ScanResult:
        ...


def read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> tuple[str, bool]:
    data = path.read_bytes()
    raw = data[:max_bytes]
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff"), len(data) > max_bytes


def _entry_key(host: str, relative: str, text: str, index: int) -> str:
    prefix = re.sub(r"\s+", " ", text.strip())[:32]
    digest = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:16]
    return f"{host}:{relative}#{digest}:{index}"


def split_markdown(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("# "):
            if current and "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current = [line.lstrip("# ").strip()]
        elif line.startswith("- "):
            if current and "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current = [line[2:].strip()]
        else:
            current.append(line)
    if current and "\n".join(current).strip():
        chunks.append("\n".join(current).strip())
    return chunks or ([text.strip()] if text.strip() else [])


def normalized_digest(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _item_root(access, path) -> str:
    resolved = Path(path).resolve()
    for root in getattr(access, "roots", ()):
        if resolved == root or root in resolved.parents:
            return str(root)
    return ""


def scan_markdown(
    access, path, *, host: str, kind: str, collection: str, whole_file: bool = False,
) -> list[ImportItem]:
    text = access.open_text(path, kind=kind)
    if whole_file:
        return [
            ImportItem(
                item_id=ItemId(kind, collection, "file"),
                text=text,
                locator=collection,
                root=_item_root(access, path),
            )
        ]
    items = []
    for index, chunk in enumerate(split_markdown(text)):
        digest = normalized_digest(chunk)
        items.append(
            ImportItem(
                item_id=ItemId(kind, collection, digest),
                text=chunk,
                locator=f"{collection}#{index + 1}",
                root=_item_root(access, path),
            )
        )
    return items


def markdown_items(
    path: Path,
    root: Path,
    *,
    host: str,
    kind: str,
    occurred_at: datetime | None = None,
    claimed_author: str | None = None,
) -> list[ImportItem]:
    text, _truncated = read_text(path)
    relative = path.relative_to(root).as_posix()
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("# "):
            if current and "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current = [line.lstrip("# ").strip()]
        elif line.startswith("- "):
            if current and "\n".join(current).strip():
                chunks.append("\n".join(current).strip())
            current = [line[2:].strip()]
        else:
            current.append(line)
    if current and "\n".join(current).strip():
        chunks.append("\n".join(current).strip())
    if not chunks and text.strip():
        chunks = [text.strip()]
    items: list[ImportItem] = []
    for index, chunk in enumerate(chunks):
        items.append(
            ImportItem(
                kind=kind,  # type: ignore[arg-type]
                text=chunk,
                source_key=_entry_key(host, relative, chunk, index),
                occurred_at=occurred_at,
                claimed_author=claimed_author,
                locator=f"{relative}#{index + 1}",
            )
        )
    return items


def _source(path: Path, kind_hint: str) -> SourceFile:
    try:
        stat = path.stat()
    except OSError:
        stat = None
    return SourceFile(
        path=path,
        kind_hint=kind_hint,
        size=stat.st_size if stat else 0,
        mtime=stat.st_mtime if stat else 0.0,
    )


READERS: dict[str, Callable[[], SourceReader]] = {}


def _register() -> None:
    from jiuwen_memory_entry.local.sync.readers.codex import CodexReader
    from jiuwen_memory_entry.local.sync.readers.dsh import DshReader
    from jiuwen_memory_entry.local.sync.readers.hermes import HermesReader
    from jiuwen_memory_entry.local.sync.readers.workbuddy import WorkBuddyReader

    READERS.update(
        {
            "codex": CodexReader,
            "dsh": DshReader,
            "hermes": HermesReader,
            "workbuddy": WorkBuddyReader,
        }
    )


_register()
