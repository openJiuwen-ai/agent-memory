# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional KV service for indexed space metadata and aggregate usage."""

import json
from typing import Protocol

from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX

UsageRow = tuple[int, int, int]


class SpaceQueries(Protocol):
    def start(self) -> None:
        ...

    def release(self) -> None:
        ...

    def metadata(self, org: str) -> list[bytes]:
        ...

    def usage(self, org: str, space: str) -> UsageRow:
        ...


def metadata_owner(raw: bytes | None) -> tuple[str, str] | None:
    record = _record_json(raw)
    if isinstance(record, dict):
        org, space = record.get("org"), record.get("space")
        if all(isinstance(part, str) and part for part in (org, space)):
            return org, space
    return None


def belongs_to_space(owner: tuple[str, str] | None, key: str, raw: bytes, *, root: bool) -> bool:
    """Use ownership recorded in recognised engine formats for a shared legacy bucket."""
    if owner is None:
        return False
    if key.startswith((MEMORY_KEY_PREFIX, MESSAGES_KEY_PREFIX)):
        record = _record_json(raw)
        if not isinstance(record, dict) or record.get("id") != key.split("/", 2)[2]:
            return False
        coords = record.get("scope")
        # Older four-axis values have no space dimension.
        return isinstance(coords, list) and len(coords) == 5 and tuple(coords[:2]) == owner
    if not root:
        return False
    if key == "/space/info":
        return metadata_owner(raw) == owner
    if key.startswith("/space/exports/"):
        record = _record_json(raw)
        return (isinstance(record, dict) and (record.get("org"), record.get("space")) == owner
                and record.get("id") == key.removeprefix("/space/exports/"))
    if key == "/space/members" or key.startswith("/space/members/"):
        entries = _record_json(raw)
        if key != "/space/members":
            entries = [entries]
        if not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            coords = entry.get("scope") if isinstance(entry, dict) else None
            if not isinstance(coords, dict) or (coords.get("org"), coords.get("space")) != owner:
                return False
        return True
    return False


def _record_json(raw: bytes | None):
    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            pass
    return None
