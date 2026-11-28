# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Codex host writer: hooks in ``~/.codex/hooks.json`` and the MCP server in config.toml."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall
from jiuwen_memory_entry.local.locks import file_lock

EVENTS = ("UserPromptSubmit", "Stop")


def codex_home() -> Path:
    return Path(os.environ.get("JIUWEN_CODEX_HOME", Path.home() / ".codex"))


MCP_SECTION = "mcp_servers.jiuwen-memory"


def _hooks_path() -> Path:
    return codex_home() / "hooks.json"


def _config_path() -> Path:
    return codex_home() / "config.toml"


def _toml_string(value: str) -> str:
    """TOML basic string: backslashes (Windows paths) and quotes must be escaped."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _mcp_block(install: HostInstall) -> str:
    interpreter = install.interpreter or sys.executable
    return (
        f"[{MCP_SECTION}]\n"
        f"command = {_toml_string(interpreter)}\n"
        f'args = ["-m", "jiuwen_memory_adapter.continuity.mcp"]\n'
        f"env = {{ JIUWEN_MEMORY_INSTALL = {_toml_string(install.install_id)} }}\n"
    )


def _split_mcp(text: str) -> tuple[str, str] | None:
    """Return (before, section) for our MCP section, or ``None`` when absent."""
    pattern = re.compile(
        r"(?ms)^\[" + re.escape(MCP_SECTION) + r"\]\r?\n.*?(?=^\[|\Z)"
    )
    match = pattern.search(text)
    if match is None:
        return None
    return text[: match.start()], match.group(0)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _command(install: HostInstall, event: str) -> str:
    return f"jiuwen-memory hook codex {event} --install {install.install_id}"


def _read(path: Path) -> dict:
    """Read the hooks file. A parse failure is an error, never an empty config.

    Treating unreadable JSON as ``{}`` would rewrite the file with only our entries and
    silently drop hooks other tools registered there.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(
            f"{path}: cannot parse the hooks file; refusing to overwrite other tools' hooks"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the hooks file is not a JSON object")
    return data


def _atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class CodexWriter:
    """Merge-only writer: other tools' hook entries are kept byte-for-byte."""

    def merge(self, install: HostInstall) -> list[EntryRef]:
        path = _hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        written: list[EntryRef] = []
        with file_lock(
            path.with_name("hooks.json.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex hooks file",
        ):
            data = _read(path)
            hooks = data.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise ValueError(f"{path}: 'hooks' is not an object")
            for event in EVENTS:
                bucket = hooks.setdefault(event, [])
                if not isinstance(bucket, list):
                    raise ValueError(f"{path}: hooks.{event} is not a list")
                command = _command(install, event)
                if any(
                    isinstance(item, dict) and item.get("command") == command
                    for item in bucket
                ):
                    continue
                entry = {"type": "command", "command": command}
                bucket.append(entry)
                written.append(
                    EntryRef(
                        path=path, section=f"hooks.{event}", value=entry, raw=command,
                        layer="hook", created_file=created,
                    )
                )
            _atomic(path, data)
        written.append(self.merge_mcp(install))
        return written

    @staticmethod
    def merge_mcp(install: HostInstall) -> EntryRef:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        with file_lock(
            path.with_name("config.toml.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex config",
        ):
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            split = _split_mcp(text)
            if split is None:
                body = text.rstrip("\r\n") + "\n\n" + _mcp_block(install)
            else:
                body = split[0] + _mcp_block(install) + text[len(split[0]) + len(split[1]):]
            _atomic_text(path, body)
        return EntryRef(
            path=path, section=MCP_SECTION, value={"install_id": install.install_id},
            raw=install.install_id, created_file=created,
        )

    def verify(self, install: HostInstall) -> bool:
        try:
            data = _read(_hooks_path())
        except (OSError, ValueError):
            return False
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return False
        for event in EVENTS:
            bucket = hooks.get(event)
            command = _command(install, event)
            if not isinstance(bucket, list) or not any(
                isinstance(item, dict) and item.get("command") == command for item in bucket
            ):
                return False
        return self.verify_mcp(install)

    @staticmethod
    def verify_mcp(install: HostInstall) -> bool:
        # CodeCheck's isort treats tomllib as third-party; import it locally so the
        # module-level stdlib block stays ordered for both linters.
        import tomllib

        try:
            data = tomllib.loads(_config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        server = (data.get("mcp_servers") or {}).get("jiuwen-memory")
        if not isinstance(server, dict):
            return False
        env = server.get("env") or {}
        return (
            isinstance(env, dict)
            and env.get("JIUWEN_MEMORY_INSTALL") == install.install_id
            and "-m" in (server.get("args") or [])
        )

    def rollback(self, install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        path = _hooks_path()
        if not path.exists():
            return list(entries)
        path.parent.mkdir(parents=True, exist_ok=True)
        kept: list[EntryRef] = []
        with file_lock(
            path.with_name("hooks.json.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex hooks file",
        ):
            try:
                data = _read(path)
            except (OSError, ValueError):
                return list(entries)
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return list(entries)
            for entry in entries:
                if entry.section == MCP_SECTION:
                    continue
                event = entry.section.removeprefix("hooks.")
                bucket = hooks.get(event)
                if not isinstance(bucket, list):
                    kept.append(entry)
                    continue
                command = entry.raw
                if any(
                    isinstance(item, dict) and item.get("command") == command for item in bucket
                ):
                    hooks[event] = [
                        item
                        for item in bucket
                        if not (isinstance(item, dict) and item.get("command") == command)
                    ]
                else:
                    # Already removed or edited by the user after connect: keep and report.
                    kept.append(entry)
            created_only = bool(entries) and entries[0].created_file
            no_other_hooks = set(data) <= {"hooks"} and not any(
                (data.get("hooks") or {}).values()
            )
            if created_only and no_other_hooks:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, data)
        kept.extend(self.rollback_mcp(install, entries))
        return kept

    @staticmethod
    def rollback_mcp(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        ours = [entry for entry in entries if entry.section == MCP_SECTION]
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not ours:
            return []
        if not path.exists():
            return list(ours)
        with file_lock(
            path.with_name("config.toml.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex config",
        ):
            text = path.read_text(encoding="utf-8")
            split = _split_mcp(text)
            if split is None:
                return list(ours)
            body = split[0].rstrip("\r\n") + "\n" if split[0].strip() else ""
            if ours[0].created_file and not body.strip():
                path.unlink(missing_ok=True)
            else:
                _atomic_text(path, body)
        return []


class CodexMcpWriter:
    """Required MCP layer: only ``config.toml``."""

    requires_plugin = False

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        return [CodexWriter.merge_mcp(install)]

    @staticmethod
    def verify(install: HostInstall) -> bool:
        return CodexWriter.verify_mcp(install)

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        return CodexWriter.rollback_mcp(install, entries)


class CodexHookWriter:
    """Optional hook layer: only ``hooks.json`` command entries."""

    requires_plugin = False

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def preflight(_install: HostInstall) -> list[str]:
        return []

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        path = _hooks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        written: list[EntryRef] = []
        with file_lock(
            path.with_name("hooks.json.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex hooks file",
        ):
            data = _read(path)
            hooks = data.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise ValueError(f"{path}: 'hooks' is not an object")
            for event in EVENTS:
                bucket = hooks.setdefault(event, [])
                if not isinstance(bucket, list):
                    raise ValueError(f"{path}: hooks.{event} is not a list")
                command = _command(install, event)
                if any(
                    isinstance(item, dict) and item.get("command") == command
                    for item in bucket
                ):
                    continue
                entry = {"type": "command", "command": command}
                bucket.append(entry)
                written.append(
                    EntryRef(
                        path=path, section=f"hooks.{event}", value=entry, raw=command,
                        layer="hook", created_file=created,
                    )
                )
            _atomic(path, data)
        return written

    @staticmethod
    def verify(install: HostInstall) -> bool:
        try:
            data = _read(_hooks_path())
        except (OSError, ValueError):
            return False
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return False
        for event in EVENTS:
            bucket = hooks.get(event)
            command = _command(install, event)
            if not isinstance(bucket, list) or not any(
                isinstance(item, dict) and item.get("command") == command for item in bucket
            ):
                return False
        return True

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        path = _hooks_path()
        if not path.exists():
            return list(entries)
        kept: list[EntryRef] = []
        with file_lock(
            path.with_name("hooks.json.lock"),
            timeout=10.0,
            conflict_message="another process is modifying the Codex hooks file",
        ):
            try:
                data = _read(path)
            except (OSError, ValueError):
                return list(entries)
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return list(entries)
            for entry in entries:
                if entry.section == MCP_SECTION:
                    continue
                event = entry.section.removeprefix("hooks.")
                bucket = hooks.get(event)
                if not isinstance(bucket, list):
                    kept.append(entry)
                    continue
                command = entry.raw
                if any(
                    isinstance(item, dict) and item.get("command") == command
                    for item in bucket
                ):
                    hooks[event] = [
                        item
                        for item in bucket
                        if not (isinstance(item, dict) and item.get("command") == command)
                    ]
                else:
                    kept.append(entry)
            _atomic(path, data)
        return kept
