"""Space-scoped serialization and recovery for sync writes and deletes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from jiuwen_memory_entry.local.locks import file_lock


def _lock_path(data_dir: Path, space: str) -> Path:
    digest = hashlib.sha256(space.encode("utf-8")).hexdigest()[:16]
    return Path(data_dir) / "locks" / f"space-{digest}.lock"


@contextmanager
def space_lock(space: str, data_dir: Path | None = None) -> Iterator[None]:
    """Cross-process lock for one target space.

    ``data_dir`` is normally the local service data directory.  The default keeps
    the helper usable from tests and tools without constructing a full home.
    """
    if isinstance(space, Path) and isinstance(data_dir, str):
        space, data_dir = data_dir, space
    base = Path(data_dir) if data_dir is not None else Path.home() / ".jiuwen-memory" / "data"
    path = _lock_path(base, str(space))
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=0.05, conflict_message="space sync already running"):
        yield


@dataclass
class SpaceRecoveryReport:
    """Outcome of replaying unconfirmed operations for one space."""

    unconfirmed: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    pending_cleanups: int = 0
    retained: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    authorization_changed: bool = False


def recover_space(security, space: str, *, engine, data_dir: Path | None = None):
    """Module-level entry used by callers that already hold a sync engine."""
    return engine.recover_space(security, space, data_dir=data_dir)
