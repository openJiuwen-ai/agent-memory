# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Hermes host writer: MCP config plus an optional MemoryProvider plugin."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall
from jiuwen_memory_entry.local.locks import file_lock

PROVIDER = "jiuwen_memory"
SERVER = "jiuwen-memory"


def hermes_home() -> Path:
    return Path(os.environ.get("JIUWEN_HERMES_HOME", Path.home() / ".hermes"))


def _config_path(install: HostInstall) -> Path:
    base = hermes_home()
    if install.profile:
        return base / "profiles" / install.profile / "config.yaml"
    return base / "config.yaml"


def _plugin_source() -> Path:
    # jiuwen_memory_adapter/hermes is shipped next to the entry package.
    return Path(__file__).resolve().parents[3] / "jiuwen_memory_adapter" / "hermes"


class HermesMcpWriter:
    """Required MCP layer under ``mcp_servers`` (per profile when requested)."""

    requires_plugin = False

    @staticmethod
    def _provider_install(install: HostInstall) -> str:
        path = _config_path(install)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return ""
        memory = data.get("memory") if isinstance(data, dict) else None
        return str((memory or {}).get("install_id", "")) if isinstance(memory, dict) else ""

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        path = _config_path(install)
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(
            path.with_name("config.yaml.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Hermes config",
        ):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except FileNotFoundError:
                data = {}
            except yaml.YAMLError as exc:
                raise ValueError(f"{path}: invalid YAML: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path}: configuration must be an object")
            key = (
                "mcpServers"
                if "mcpServers" in data and "mcp_servers" not in data
                else "mcp_servers"
            )
            servers = data.setdefault(key, {})
            if not isinstance(servers, dict):
                raise ValueError(f"{path}: '{key}' is not an object")
            servers[SERVER] = {
                "command": install.interpreter or "python",
                "args": ["-m", "jiuwen_memory_adapter.continuity.mcp"],
                "env": {"JIUWEN_MEMORY_INSTALL": install.install_id},
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            os.replace(temporary, path)
        return [
            EntryRef(
                path=path, section=f"{key}.jiuwen-memory",
                value={"install_id": install.install_id}, raw=install.install_id,
                layer="mcp", created_file=created,
            )
        ]

    @staticmethod
    def verify(install: HostInstall) -> bool:
        try:
            data = yaml.safe_load(_config_path(install).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return False
        servers = data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = data.get("mcpServers")
        entry = (servers or {}).get(SERVER)
        return (
            isinstance(entry, dict)
            and (entry.get("env") or {}).get("JIUWEN_MEMORY_INSTALL")
            == install.install_id
        )

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        path = _config_path(install)
        if not path.exists():
            return list(entries)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return list(entries)
        key = (
            "mcpServers"
            if isinstance(data, dict)
            and "mcpServers" in data
            and "mcp_servers" not in data
            else "mcp_servers"
        )
        servers = data.get(key) if isinstance(data, dict) else None
        entry = servers.get(SERVER) if isinstance(servers, dict) else None
        if (
            not isinstance(entry, dict)
            or (entry.get("env") or {}).get("JIUWEN_MEMORY_INSTALL")
            != install.install_id
        ):
            return list(entries)
        servers.pop(SERVER, None)
        created_only = bool(entries) and entries[0].created_file
        no_other_servers = set(data) <= {"mcp_servers", "mcpServers"} and not servers
        if created_only and no_other_servers:
            path.unlink(missing_ok=True)
            return []
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        os.replace(temporary, path)
        return []


class HermesHookWriter:
    """Optional MemoryProvider plugin; enabling replaces any existing provider."""

    requires_plugin = True

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def _provider(install: HostInstall) -> str:
        path = _config_path(install)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return ""
        memory = data.get("memory") if isinstance(data, dict) else None
        return str((memory or {}).get("provider", "")) if isinstance(memory, dict) else ""

    def preflight(self, install: HostInstall) -> list[str]:
        current = self._provider(install)
        messages = [f"will install the Hermes MemoryProvider plugin into {hermes_home()}"]
        if current and current != PROVIDER:
            messages.append(f"will replace the existing memory provider {current!r}")
        return messages

    @staticmethod
    def _provider_install(install: HostInstall) -> str:
        path = _config_path(install)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return ""
        memory = data.get("memory") if isinstance(data, dict) else None
        return str((memory or {}).get("install_id", "")) if isinstance(memory, dict) else ""

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        path = _config_path(install)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(
            path.with_name("config.yaml.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Hermes config",
        ):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except FileNotFoundError:
                data = {}
            except yaml.YAMLError as exc:
                raise ValueError(f"{path}: invalid YAML: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path}: configuration must be an object")
            memory = data.setdefault("memory", {})
            if not isinstance(memory, dict):
                raise ValueError(f"{path}: 'memory' is not an object")
            previous = str(memory.get("provider", ""))
            previous_install = str(memory.get("install_id", ""))
            previous_profile = str(memory.get("profile", ""))
            memory["provider"] = PROVIDER
            memory["install_id"] = install.install_id
            memory["profile"] = install.profile
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            os.replace(temporary, path)
        target = hermes_home() / "plugins" / PROVIDER
        source = _plugin_source()
        if not source.is_dir():
            raise ValueError(f"Hermes plugin source not found: {source}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return [
            EntryRef(
                path=path, section="memory.provider",
                value={
                    "previous": previous,
                    "previous_install_id": previous_install,
                    "previous_profile": previous_profile,
                    "installed": PROVIDER,
                },
                raw=PROVIDER, layer="hook",
            ),
            EntryRef(
                path=target, section="plugin", value={"provider": PROVIDER},
                raw=PROVIDER, layer="hook",
            ),
        ]

    def verify(self, install: HostInstall) -> bool:
        return (
            self._provider(install) == PROVIDER
            and self._provider_install(install) == install.install_id
            and (hermes_home() / "plugins" / PROVIDER).is_dir()
        )

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        kept = [entry for entry in entries if entry.section == "plugin"]
        path = _config_path(install)
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            memory = data.get("memory") if isinstance(data, dict) else None
            if isinstance(memory, dict) and memory.get("provider") == PROVIDER:
                previous = ""
                previous_install = ""
                previous_profile = ""
                for entry in entries:
                    if entry.section == "memory.provider" and isinstance(entry.value, dict):
                        previous = str(entry.value.get("previous", ""))
                        previous_install = str(
                            entry.value.get("previous_install_id", "")
                        )
                        previous_profile = str(entry.value.get("previous_profile", ""))
                if previous:
                    memory["provider"] = previous
                else:
                    memory.pop("provider", None)
                if previous_install:
                    memory["install_id"] = previous_install
                else:
                    memory.pop("install_id", None)
                if previous_profile:
                    memory["profile"] = previous_profile
                else:
                    memory.pop("profile", None)
                temporary = path.with_name(path.name + ".tmp")
                temporary.write_text(
                    yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
        plugin = hermes_home() / "plugins" / PROVIDER
        if plugin.exists() and not _other_profile_uses_provider(install):
            shutil.rmtree(plugin)
        return [entry for entry in kept if entry.path != plugin]


def _config_uses_provider(path: Path) -> bool:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    memory = data.get("memory") if isinstance(data, dict) else None
    return isinstance(memory, dict) and memory.get("provider") == PROVIDER


def _other_profile_uses_provider(current: HostInstall) -> bool:
    base = hermes_home()
    if current.profile and _config_uses_provider(base / "config.yaml"):
        return True
    profiles = base / "profiles"
    if not profiles.is_dir():
        return False
    for config in profiles.glob("*/config.yaml"):
        if current.profile and config.parent.name == current.profile:
            continue
        if _config_uses_provider(config):
            return True
    return False
