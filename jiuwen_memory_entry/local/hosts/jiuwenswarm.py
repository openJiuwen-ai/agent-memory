# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""JiuwenSwarm host writer: switch the provider to the local service connection.

JiuwenSwarm owns its configuration file; when ``JIUWEN_SWARM_CONFIG`` points at it the
writer merges ``memory.builtin.jiuwen`` in place. Without a path the writer records a
ready-to-paste snippet next to the install and still counts as written (the snippet is
the manual step reported by ``connect``).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from jiuwen_memory_entry.local.hosts import EntryRef, HostInstall


def swarm_config() -> Path | None:
    value = os.environ.get("JIUWEN_SWARM_CONFIG", "").strip()
    return Path(value).expanduser() if value else None


def _snippet(install: HostInstall) -> dict:
    return {
        "memory": {
            "builtin": {
                "jiuwen": {
                    "connection": "local",
                    "install_id": install.install_id,
                    "project": install.project,
                }
            }
        }
    }


def _read_config(config: Path) -> dict:
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if config.suffix in {".yml", ".yaml"}:
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    return data if isinstance(data, dict) else {}


def _write_config(config: Path, data: dict) -> None:
    if config.suffix in {".yml", ".yaml"}:
        import yaml

        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(data, indent=2) + "\n"
    temporary = config.with_name(config.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, config)


class JiuwenSwarmWriter:
    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        config = swarm_config()
        if config is None:
            # No config path: ``install`` records the snippet next to the install and
            # reports the manual step; the entry is still verifiable.
            return [
                EntryRef(
                    path=Path("-"),
                    section="manual",
                    value=_snippet(install),
                    raw=install.install_id,
                )
            ]
        data = _read_config(config)
        previous = copy.deepcopy(
            ((data.get("memory") or {}).get("builtin") or {}).get("jiuwen")
        )
        snippet = _snippet(install)
        written = dict(snippet["memory"]["builtin"]["jiuwen"])
        data = _deep_merge(data, snippet)
        _write_config(config, data)
        return [
            EntryRef(
                path=config,
                section="memory.builtin.jiuwen",
                value={"previous": previous, "written": written,
                       "install_id": install.install_id},
                raw=install.install_id,
            )
        ]

    @staticmethod
    def verify(install: HostInstall) -> bool:
        config = swarm_config()
        if config is None or not config.exists():
            return False
        try:
            if config.suffix in {".yml", ".yaml"}:
                import yaml

                data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            else:
                data = json.loads(config.read_text(encoding="utf-8") or "{}")
        except Exception:
            return False
        block = ((data.get("memory") or {}).get("builtin") or {}).get("jiuwen")
        return isinstance(block, dict) and block.get("install_id") == install.install_id

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        ours = [entry for entry in entries if entry.section == "memory.builtin.jiuwen"]
        config = swarm_config()
        if not ours:
            return []
        if config is None or not config.exists():
            return list(ours)
        data = _read_config(config)
        block = (data.get("memory") or {}).get("builtin") or {}
        current = block.get("jiuwen")
        if not isinstance(current, dict) or current.get("install_id") != install.install_id:
            # The user edited the block after connect: keep it and report.
            return list(ours)
        stored = ours[0].value if isinstance(ours[0].value, dict) else {}
        previous = stored.get("previous")
        written = stored.get("written") if isinstance(stored.get("written"), dict) else {}
        # Restore only the fields this install wrote and that the user has not edited
        # since; a snapshot replace would discard unrelated changes to the block.
        for field, installed_value in written.items():
            if current.get(field) != installed_value:
                continue
            if isinstance(previous, dict) and field in previous:
                current[field] = previous[field]
            else:
                current.pop(field, None)
        if current:
            block["jiuwen"] = current
        else:
            block.pop("jiuwen", None)
        data.setdefault("memory", {})["builtin"] = block
        _write_config(config, data)
        return []


def _deep_merge(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
