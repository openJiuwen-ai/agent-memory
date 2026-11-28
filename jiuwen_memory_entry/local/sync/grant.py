"""Sync authorization: roots, types, target space and a monotonically versioned grant."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TYPES = frozenset({"profile", "fact", "daily_log", "session_turn"})


@dataclass
class SyncGrant:
    grant_id: str
    version: int
    roots: tuple[str, ...]
    types: frozenset[str]
    target_space: str
    visible_snapshot: tuple[str, ...] = ()
    mirror_deletes: bool = False
    interval: int = 0
    revoked: bool = False
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_dict(cls, data: dict) -> SyncGrant | None:
        if not isinstance(data, dict) or not data.get("grant_id"):
            return None
        types = frozenset(str(item) for item in data.get("types") or ())
        return cls(
            grant_id=str(data["grant_id"]),
            version=int(data.get("version", 1)),
            roots=tuple(str(item) for item in data.get("roots") or ()),
            types=types & TYPES,
            target_space=str(data.get("target_space", "default")),
            visible_snapshot=tuple(str(item) for item in data.get("visible_snapshot") or ()),
            mirror_deletes=bool(data.get("mirror_deletes", False)),
            interval=int(data.get("interval", 0) or 0),
            revoked=bool(data.get("revoked", False)),
            updated_at=str(data.get("updated_at", "")),
        )

    def as_dict(self) -> dict:
        return {
            "allowed": not self.revoked,
            "grant_id": self.grant_id,
            "version": self.version,
            "roots": list(self.roots),
            "types": sorted(self.types),
            "target_space": self.target_space,
            "visible_snapshot": list(self.visible_snapshot),
            "mirror_deletes": self.mirror_deletes,
            "interval": self.interval,
            "revoked": self.revoked,
            "updated_at": self.updated_at,
        }


def new_grant(
    roots: list[str], *, types: set[str] | None, target_space: str,
    visible_snapshot: list[str] | None = None, mirror_deletes: bool = False,
    interval: int = 0,
) -> SyncGrant:
    selected = frozenset(types or {"profile", "fact", "daily_log"})
    if not selected <= TYPES:
        raise ValueError(f"unknown sync types: {sorted(selected - TYPES)}")
    return SyncGrant(
        grant_id=secrets.token_hex(16),
        version=1,
        roots=tuple(str(Path(item).expanduser().resolve()) for item in roots),
        types=selected,
        target_space=target_space,
        visible_snapshot=tuple(visible_snapshot or ()),
        mirror_deletes=mirror_deletes,
        interval=interval,
    )


def change_grant(grant: SyncGrant, **changes) -> SyncGrant:
    data = grant.as_dict()
    data.update(changes)
    updated = SyncGrant.from_dict(data)
    if updated is None:
        raise ValueError("invalid grant")
    updated.version = grant.version + 1
    updated.grant_id = grant.grant_id
    return updated


def revoke(grant: SyncGrant) -> SyncGrant:
    """Revoke the grant, keeping its identity and advancing the version."""
    return change_grant(grant, revoked=True)
