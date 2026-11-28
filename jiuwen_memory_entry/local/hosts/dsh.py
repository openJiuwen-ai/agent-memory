# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DSH host writer: install the plugin into both profiles and verify via the host CLI.

DSH owns its profile plugin list, so the writer asks ``dsh plugin`` to add the package
and only reports success when the host lists it for every profile. The local JSON file
is a record for ``disconnect``, never proof that the host loaded the plugin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall

PROFILES = ("desktop", "dsh-tui")
PACKAGE = "@jiuwen/dsh-memory"


def dsh_home() -> Path:
    return Path(os.environ.get("JIUWEN_DSH_HOME", Path.home() / ".dsh"))


def _path() -> Path:
    return dsh_home() / "jiuwen-memory.json"


def _run(args: list[str]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _install_profile(profile: str) -> bool:
    """Both argument orders of ``dsh plugin add`` shipped; either one may win."""
    attempts = (
        ["dsh", "plugin", "--profile", profile, "add", PACKAGE],
        ["dsh", "plugin", "add", PACKAGE, "--profile", profile],
    )
    return any(_run(args) for args in attempts)


def _plugin_removed(profile: str) -> bool:
    for args in (
        ["dsh", "plugin", "--profile", profile, "remove", PACKAGE],
        ["dsh", "plugin", "remove", PACKAGE, "--profile", profile],
    ):
        if _run(args):
            return True
    return False


def _plugin_listed(profile: str) -> bool | None:
    """True/False when the host answered, None when the query itself failed."""
    answered = False
    for args in (
        ["dsh", "plugin", "--profile", profile, "list"],
        ["dsh", "plugin", "list", "--profile", profile],
    ):
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            answered = True
            if PACKAGE in result.stdout:
                return True
    return False if answered else None


def _profiles(install: HostInstall) -> tuple[str, ...]:
    return (install.profile,) if install.profile else PROFILES


class DshWriter:
    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        existing_added: list[str] = []
        path = _path()
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if (
            isinstance(previous, dict)
            and previous.get("install_id") == install.install_id
            and isinstance(previous.get("added_profiles"), list)
        ):
            existing_added = [str(item) for item in previous.get("added_profiles", [])]
        added: list[str] = []
        for profile in _profiles(install):
            listed = _plugin_listed(profile)
            if listed is None:
                raise ValueError(
                    f"cannot determine whether {PACKAGE} is installed in DSH profile "
                    f"{profile!r}; refusing to guess ownership"
                )
            if listed:
                # The profile already had the plugin before this install: never remove
                # it on disconnect.
                continue
            if not _install_profile(profile):
                cleanup_failed = [
                    item for item in added if not _plugin_removed(item)
                ]
                detail = (
                    f"; cleanup failed for {cleanup_failed}" if cleanup_failed else ""
                )
                raise ValueError(
                    f"could not install {PACKAGE} into DSH profile {profile!r}{detail}; "
                    f"run `dsh plugin --profile {profile} add {PACKAGE}` and retry"
                )
            added.append(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        added = sorted(set(existing_added) | set(added))
        payload = {
            "install_id": install.install_id,
            "package": PACKAGE,
            "profiles": list(_profiles(install)),
            "added_profiles": added,
            "service": "local",
        }
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return [EntryRef(path=path, section="plugin", value=payload, raw=PACKAGE)]

    @staticmethod
    def verify(install: HostInstall) -> bool:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("install_id") != install.install_id:
            return False
        return all(_plugin_listed(profile) is True for profile in PROFILES)

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        path = _path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return list(entries)
        if data.get("install_id") != install.install_id:
            return list(entries)  # edited by the user after connect: keep and report
        stored = entries[0].value if entries and isinstance(entries[0].value, dict) else {}
        # Old records did not track per-profile additions; keep the legacy behavior for
        # them, but new records remove exactly the profiles this install added.
        added = stored.get("added_profiles")
        profiles = list(added) if isinstance(added, list) else list(PROFILES)
        if not all(_plugin_removed(profile) for profile in profiles):
            return list(entries)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return []


class DshHookWriter(DshWriter):
    """Turns the Cordis plugin installation into the optional hook layer."""

    requires_plugin = True

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def preflight(_install: HostInstall) -> list[str]:
        return [f"will install {PACKAGE} into DSH profiles {', '.join(PROFILES)}"]


class DshMcpWriter:
    """Required MCP layer: one ``dsh-mcp-client`` plugin entry per profile."""

    requires_plugin = False

    @staticmethod
    def _profile_files(profile: str = "") -> list[tuple[str, Path]]:
        base = dsh_home() / "profiles"
        names = (profile,) if profile else PROFILES
        found = [
            (name, base / name / "cordis.patch.yml")
            for name in names
            if (base / name).is_dir()
        ]
        if not found:
            raise ValueError(f"no DSH profiles found under {base}")
        return found

    @staticmethod
    def _entry(install: HostInstall) -> dict:
        return {
            "id": f"jiuwen-memory-{install.install_id[:8]}",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": "jiuwen-memory",
                "transport": "stdio",
                "command": install.interpreter or sys.executable,
                "args": ["-m", "jiuwen_memory_adapter.continuity.mcp"],
                "env": {"JIUWEN_MEMORY_INSTALL": install.install_id},
            },
        }

    def merge(self, install: HostInstall) -> list[EntryRef]:
        entries: list[EntryRef] = []
        for profile, path in self._profile_files(install.profile):
            path.parent.mkdir(parents=True, exist_ok=True)
            created = not path.exists()
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
            except yaml.YAMLError as exc:
                raise ValueError(f"{path}: invalid YAML: {exc}") from exc
            if data is None:
                data = []
            if not isinstance(data, list):
                raise ValueError(f"{path}: DSH patch must be a list of plugin entries")
            our_id = f"jiuwen-memory-{install.install_id[:8]}"
            data = [
                item for item in data
                if not (isinstance(item, dict) and item.get("id") == our_id)
            ]
            data.append(self._entry(install))
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            os.replace(temporary, path)
            entries.append(
                EntryRef(
                    path=path, section=f"dsh-mcp-client.{our_id}",
                    value={"profile": profile, "install_id": install.install_id, "id": our_id},
                    raw=install.install_id, layer="mcp", created_file=created,
                )
            )
        return entries

    def verify(self, install: HostInstall) -> bool:
        try:
            files = self._profile_files(install.profile)
        except ValueError:
            return False
        our_id = f"jiuwen-memory-{install.install_id[:8]}"
        for _profile, path in files:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            except (OSError, yaml.YAMLError):
                return False
            if not isinstance(data, list):
                return False
            entry = next(
                (item for item in data if isinstance(item, dict) and item.get("id") == our_id),
                None,
            )
            if entry is None:
                return False
            config = entry.get("config") if isinstance(entry, dict) else None
            if not isinstance(config, dict):
                return False
            if (config.get("env") or {}).get("JIUWEN_MEMORY_INSTALL") != install.install_id:
                return False
        return True

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        kept: list[EntryRef] = []
        for entry in entries:
            path = entry.path
            if not path.exists():
                kept.append(entry)
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            except (OSError, yaml.YAMLError):
                kept.append(entry)
                continue
            if not isinstance(data, list):
                kept.append(entry)
                continue
            our_id = str((entry.value or {}).get("id", ""))
            kept_data = [
                item for item in data
                if not (isinstance(item, dict) and item.get("id") == our_id)
            ]
            if len(kept_data) == len(data):
                kept.append(entry)
                continue
            if entry.created_file and not kept_data:
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                yaml.safe_dump(kept_data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            os.replace(temporary, path)
        return kept
