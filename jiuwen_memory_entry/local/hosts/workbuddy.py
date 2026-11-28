# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""WorkBuddy MCP writer with three-state detection and effective-entry checks."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall
from jiuwen_memory_entry.local.locks import file_lock

SERVER_PREFIX = "jiuwen-memory-"


@dataclass(frozen=True)
class WorkBuddyDetection:
    state: Literal["absent", "no_config", "configured"]
    edition: str
    home: Path | None
    mcp_paths: dict[str, Path] = field(default_factory=dict)


_EDITION_HOMES = {
    "community": ("JIUWEN_WORKBUDDY_HOME", ".workbuddy"),
    "enterprise": ("JIUWEN_CODEBUDDY_HOME", ".codebuddy"),
}


def detect_workbuddy(
    project_root: str | Path | None = None, edition: str = ""
) -> WorkBuddyDetection:
    """Detect the requested edition; community comes before enterprise when unnamed."""
    if edition in _EDITION_HOMES:
        return _detect_edition(edition, project_root)
    community = _detect_edition("community", project_root)
    if community.state != "absent":
        return community
    return _detect_edition("enterprise", project_root)


def _detect_edition(
    edition: str, project_root: str | Path | None
) -> WorkBuddyDetection:
    variable, fallback = _EDITION_HOMES[edition]
    home = Path(os.environ.get(variable, Path.home() / fallback)).expanduser()
    if not home.is_dir():
        return WorkBuddyDetection("absent", "", None, {})
    if edition == "community":
        paths = {"user": home / "mcp.json"}
        state = "configured" if paths["user"].exists() else "no_config"
        return WorkBuddyDetection(state, "community", home.resolve(), paths)
    paths = {"user": home / ".mcp.json"}
    if project_root:
        root = Path(project_root).expanduser().resolve()
        preferred = root / ".codebuddy" / ".mcp.json"
        alternate = root / ".mcp.json"
        paths["project"] = alternate if alternate.exists() else preferred
    state = "configured" if any(path.exists() for path in paths.values()) else "no_config"
    return WorkBuddyDetection(state, "enterprise", home.resolve(), paths)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: configuration must be an object")
    return data


def effective_server(
    detection: WorkBuddyDetection, name: str
) -> tuple[str, dict] | None:
    """Return the highest-priority scope and entry (local > project > user)."""
    for scope in ("local", "project", "user"):
        path = detection.mcp_paths.get(scope)
        if path is None or not path.exists():
            continue
        servers = _read(path).get("mcpServers")
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            return scope, servers[name]
    return None


class WorkBuddyMcpWriter:
    requires_plugin = False

    @staticmethod
    def _detection(install: HostInstall) -> WorkBuddyDetection:
        detection = detect_workbuddy(install.project_root or None, install.edition)
        if detection.state == "absent":
            raise ValueError("WorkBuddy not detected; refusing to guess a config path")
        if install.edition and install.edition != detection.edition:
            raise ValueError(
                f"WorkBuddy edition {install.edition!r} requested but detected "
                f"{detection.edition!r}"
            )
        return detection

    @staticmethod
    def _server_name(install: HostInstall) -> str:
        suffix = hashlib.sha256(install.install_id.encode("utf-8")).hexdigest()[:8]
        return SERVER_PREFIX + suffix

    @staticmethod
    def _path(detection: WorkBuddyDetection, install: HostInstall) -> tuple[str, Path]:
        scope = install.scope or "user"
        if scope == "project" and "project" not in detection.mcp_paths:
            raise ValueError("project scope requires --project-root inside the project")
        return scope, detection.mcp_paths[scope]

    def merge(self, install: HostInstall) -> list[EntryRef]:
        detection = self._detection(install)
        scope, path = self._path(detection, install)
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        name = self._server_name(install)
        with file_lock(
            path.with_name(path.name + ".lock"),
            timeout=10.0,
            conflict_message="another process is modifying the WorkBuddy MCP config",
        ):
            data = _read(path)
            servers = data.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError(f"{path}: 'mcpServers' is not an object")
            servers[name] = {
                "command": install.interpreter or "python",
                "args": ["-m", "jiuwen_memory_adapter.continuity.mcp"],
                "env": {"JIUWEN_MEMORY_INSTALL": install.install_id},
            }
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        return [
            EntryRef(
                path=path,
                section=f"mcpServers.{name}",
                value={
                    "install_id": install.install_id,
                    "scope": scope,
                    "name": name,
                    "edition": detection.edition,
                },
                raw=name,
                layer="mcp",
                created_file=created,
            )
        ]

    def verify(self, install: HostInstall) -> bool:
        try:
            detection = self._detection(install)
            name = self._server_name(install)
            effective = effective_server(detection, name)
        except (OSError, ValueError):
            return False
        if effective is None:
            return False
        _scope, entry = effective
        expected_scope, _path = self._path(detection, install)
        return (
            (entry.get("env") or {}).get("JIUWEN_MEMORY_INSTALL") == install.install_id
            and _scope == expected_scope
        )

    def rollback(self, install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        kept: list[EntryRef] = []
        for entry in entries:
            path = entry.path
            if not path.exists():
                kept.append(entry)
                continue
            try:
                data = _read(path)
            except (OSError, ValueError):
                kept.append(entry)
                continue
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            name = str((entry.value or {}).get("name", self._server_name(install)))
            current = servers.get(name) if isinstance(servers, dict) else None
            if (
                not isinstance(current, dict)
                or (current.get("env") or {}).get("JIUWEN_MEMORY_INSTALL")
                != install.install_id
            ):
                kept.append(entry)
                continue
            servers.pop(name, None)
            if entry.created_file and set(data) <= {"mcpServers"} and not servers:
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        return kept


class WorkBuddyHookWriter:
    """Hook events for WorkBuddy are unverified; enabling must be refused."""

    requires_plugin = True

    @staticmethod
    def available() -> bool:
        return False

    @staticmethod
    def preflight(_install: HostInstall) -> list[str]:
        return ["WorkBuddy hook events have not been verified in the selected version"]

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        raise ValueError("WorkBuddy hook events have not been verified")

    @staticmethod
    def verify(install: HostInstall) -> bool:
        return False

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        return list(entries)
