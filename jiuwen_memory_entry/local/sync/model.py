"""Import item and report models shared by readers and the sync engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

ImportKind = Literal["profile", "fact", "daily_log", "session_turn"]


@dataclass(frozen=True)
class ItemId:
    kind: str
    collection: str
    inner: str

    def __str__(self) -> str:
        return f"{self.kind}|{self.collection}|{self.inner}"


@dataclass(frozen=True)
class RangeKey:
    root: str
    kind: str


@dataclass(frozen=True)
class SourceFile:
    path: Path
    kind_hint: str
    size: int
    mtime: float


@dataclass(frozen=True)
class ImportItem:
    item_id: ItemId | None = None
    text: str = ""
    occurred_at: datetime | None = None
    claimed_author: str | None = None
    locator: str = ""
    kind: ImportKind | None = None
    source_key: str = ""
    root: str = ""

    def __post_init__(self) -> None:
        if self.item_id is not None:
            object.__setattr__(self, "kind", self.item_id.kind)  # type: ignore[arg-type]
            object.__setattr__(self, "source_key", str(self.item_id))
        elif self.kind is not None and not self.source_key:
            object.__setattr__(self, "source_key", f"{self.kind}||")

    @property
    def frozen_item_id(self) -> ItemId:
        if self.item_id is not None:
            return self.item_id
        kind, _, collection = self.source_key.partition("|")
        return ItemId(self.kind or kind or "fact", collection, self.source_key)


@dataclass
class ScanResult:
    items: list[ImportItem] = field(default_factory=list)
    complete: dict[RangeKey, bool] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    deleted_in_source: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    unrecognized: bool = False
    writes: dict[str, str] = field(default_factory=dict)
    deletes: set[str] = field(default_factory=set)
    plan_id: str = ""

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "deleted_in_source": list(self.deleted_in_source),
            "failed": [list(item) for item in self.failed],
            "truncated": list(self.truncated),
            "unrecognized": self.unrecognized,
            "deletes": sorted(self.deletes),
            "plan_id": getattr(self, "plan_id", ""),
        }
