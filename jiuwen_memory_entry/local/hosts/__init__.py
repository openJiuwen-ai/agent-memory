# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host writers for ``connect``: merge this tool's entries into host configuration.

The local service owns memory; each host gets one install with its own principal and
token. Host files are merged — existing entries written by other tools are preserved
verbatim, and rollback only removes entries this tool wrote.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from jiuwen_memory_entry.local.locks import file_lock


@dataclass(frozen=True)
class HostInstall:
    """What a host writer needs to know about one installation."""

    install_id: str
    host: str
    connector_id: str
    project: str
    interpreter: str = ""
    profile: str = ""
    edition: str = ""
    scope: str = ""
    project_root: str = ""


@dataclass(frozen=True)
class EntryRef:
    """One merged host entry, enough to verify and roll it back."""

    path: Path
    section: str
    value: object
    raw: str = field(default="")
    layer: str = "mcp"
    created_file: bool = False


class HostWriter(Protocol):
    def merge(self, install: HostInstall) -> list[EntryRef]:
        ...

    def verify(self, install: HostInstall) -> bool:
        ...

    def rollback(self, install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        """Remove this tool's entries; return entries kept because the user changed them."""
        ...


def _read_json(path: Path) -> dict:
    """Read a host config file. A broken file is an error, never an empty config.

    Returning ``{}`` here would rewrite the file with only our entries and drop every
    other server or hook the host's other tools registered.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(
            f"{path}: cannot parse the host config; refusing to overwrite other tools' entries"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the host config is not a JSON object")
    return data


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _merge_json(path: Path, section: str, entries: list[dict], *, key: str) -> list[EntryRef]:
    """Append entries under ``section`` that are not already present (by ``key``)."""
    written: list[EntryRef] = []
    with file_lock(path.with_name(path.name + ".lock"), timeout=10.0,
                   conflict_message=f"{path} is being modified by another process"):
        data = _read_json(path)
        bucket = data.setdefault(section, [])
        if not isinstance(bucket, list):
            raise ValueError(f"{path}: '{section}' is not a list")
        existing = {
            json.dumps(item.get(key), sort_keys=True)
            for item in bucket
            if isinstance(item, dict)
        }
        for entry in entries:
            marker = json.dumps(entry.get(key), sort_keys=True)
            if marker in existing:
                continue
            bucket.append(entry)
            existing.add(marker)
            written.append(EntryRef(path=path, section=section, value=entry, raw=marker))
        _atomic_write(path, data)
    return written


def _rollback_json(path: Path, section: str, markers: set[str], *, key: str) -> list[EntryRef]:
    kept: list[EntryRef] = []
    with file_lock(path.with_name(path.name + ".lock"), timeout=10.0,
                   conflict_message=f"{path} is being modified by another process"):
        try:
            data = _read_json(path)
        except (OSError, ValueError):
            return list(kept)
        bucket = data.get(section)
        if not isinstance(bucket, list):
            return kept
        remaining = []
        for item in bucket:
            marker = (
                json.dumps(item.get(key), sort_keys=True) if isinstance(item, dict) else ""
            )
            if marker in markers:
                continue
            remaining.append(item)
        if len(remaining) != len(bucket):
            data[section] = remaining
            _atomic_write(path, data)
        # Entries whose marker is still present were modified by the user after merge;
        # keep them and report.
        live = {
            json.dumps(item.get(key), sort_keys=True)
            for item in remaining
            if isinstance(item, dict)
        }
        for marker in markers:
            if marker in live:
                kept.append(EntryRef(path=path, section=section, value=marker, raw=marker))
    return kept


Cap = Literal["supported", "unverified", "unsupported"]


@dataclass(frozen=True)
class HostCapabilities:
    mcp: Cap
    auto_recall: Cap
    auto_capture: Cap
    memory_import: Cap
    session_import: Cap


CAPABILITIES: dict[str, HostCapabilities] = {
    "codex": HostCapabilities("supported", "supported", "supported", "supported", "supported"),
    "dsh": HostCapabilities(
        "supported", "supported", "supported", "unsupported", "supported"
    ),
    "hermes": HostCapabilities(
        "supported", "supported", "supported", "supported", "unverified"
    ),
    "workbuddy": HostCapabilities(
        "supported", "unverified", "unverified", "supported", "unsupported"
    ),
    "jiuwenswarm": HostCapabilities(
        "supported", "supported", "supported", "supported", "supported"
    ),
}


class HookWriter(HostWriter, Protocol):
    """Optional automatic-recall layer for a host."""

    requires_plugin: bool

    def available(self) -> bool:
        ...

    def preflight(self, install: HostInstall) -> list[str]:
        """Human-readable confirmations required before enabling this hook."""
        ...


# Writer maps live in registry; this package only re-exports them lazily so the
# capability matrix and the host inventory cannot drift into two copies.


def __getattr__(name: str):
    if name in {"MCP_WRITERS", "HOOK_WRITERS", "HOOK_DEFAULTS"}:
        from jiuwen_memory_entry.local.hosts import registry

        maps = {
            "MCP_WRITERS": registry.MCP_WRITERS,
            "HOOK_WRITERS": registry.HOOK_WRITERS,
            "HOOK_DEFAULTS": registry.HOOK_DEFAULTS,
        }
        value = maps.get(name)
        if value is None:
            raise AttributeError(name)
        globals()[name] = value
        return value
    raise AttributeError(name)
