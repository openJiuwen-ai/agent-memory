# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host registry: adding a host means adding one entry, not editing install/CLI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jiuwen_memory_entry.local.hosts import (
    CAPABILITIES,
    HookWriter,
    HostCapabilities,
    HostWriter,
)


@dataclass(frozen=True)
class HostSpec:
    """Everything the installer needs to know about one host."""

    name: str
    capabilities: HostCapabilities
    mcp_writer: Callable[[], HostWriter]
    hook_writer: Callable[[], HookWriter] | None = None
    default_hooks: bool = False
    native_hooks: bool = False
    manual_ok: bool = False
    syncable: bool = True
    instance: Callable[..., dict] | None = None
    upgrade_instance: Callable[..., dict | None] | None = None


def _codex_instance(**_kwargs) -> dict:
    from jiuwen_memory_entry.local.hosts.codex import codex_home

    return {"config_root": str(codex_home().resolve())}


def _dsh_instance(profile: str = "", **_kwargs) -> dict:
    from jiuwen_memory_entry.local.hosts.dsh import dsh_home

    return {
        "config_root": str(dsh_home().resolve()),
        "profile": profile or "default",
    }


def _hermes_instance(profile: str = "", **_kwargs) -> dict:
    from jiuwen_memory_entry.local.hosts.hermes import hermes_home

    return {
        "config_root": str(hermes_home().resolve()),
        "profile": profile or "default",
    }


def _workbuddy_instance(
    edition: str = "", scope: str = "", project_root: str = "", **_kwargs
) -> dict:
    from jiuwen_memory_entry.local.hosts.workbuddy import detect_workbuddy

    layout = detect_workbuddy(project_root or None)
    return {
        "config_root": str((layout.home if layout else Path.home()).resolve()),
        "edition": edition or (layout.edition if layout else ""),
        "scope": scope or "user",
        "project_root": str(Path(project_root).expanduser().resolve())
        if project_root
        else "",
    }


def _swarm_instance(**_kwargs) -> dict:
    return {"config_root": str(Path.home().resolve())}


def _dsh_upgrade(entries: list[dict], choose_profile) -> dict | None:
    profiles: set[str] = set()
    for entry in entries:
        parts = Path(str(entry.get("path", ""))).parts
        if "profiles" in parts:
            index = parts.index("profiles")
            if index + 1 < len(parts):
                profiles.add(parts[index + 1])
    if len(profiles) == 1:
        chosen = next(iter(profiles))
    else:
        chosen = choose_profile(sorted(profiles))
        if chosen is None:
            return None
    return {
        "config_root": _dsh_instance()["config_root"],
        "profile": chosen,
    }


def _build_specs() -> dict[str, HostSpec]:
    from jiuwen_memory_entry.local.hosts.codex import (
        CodexHookWriter,
        CodexMcpWriter,
    )
    from jiuwen_memory_entry.local.hosts.dsh import DshHookWriter, DshMcpWriter
    from jiuwen_memory_entry.local.hosts.hermes import (
        HermesHookWriter,
        HermesMcpWriter,
    )
    from jiuwen_memory_entry.local.hosts.jiuwenswarm import JiuwenSwarmWriter
    from jiuwen_memory_entry.local.hosts.workbuddy import (
        WorkBuddyHookWriter,
        WorkBuddyMcpWriter,
    )

    specs = {
        "codex": HostSpec(
            "codex", CAPABILITIES["codex"], CodexMcpWriter, CodexHookWriter,
            default_hooks=True, instance=_codex_instance,
        ),
        "dsh": HostSpec(
            "dsh", CAPABILITIES["dsh"], DshMcpWriter, DshHookWriter,
            instance=_dsh_instance, upgrade_instance=_dsh_upgrade,
        ),
        "hermes": HostSpec(
            "hermes", CAPABILITIES["hermes"], HermesMcpWriter, HermesHookWriter,
            instance=_hermes_instance,
        ),
        "workbuddy": HostSpec(
            "workbuddy", CAPABILITIES["workbuddy"], WorkBuddyMcpWriter,
            WorkBuddyHookWriter, instance=_workbuddy_instance,
        ),
        "jiuwenswarm": HostSpec(
            "jiuwenswarm", CAPABILITIES["jiuwenswarm"], JiuwenSwarmWriter,
            default_hooks=True, native_hooks=True, manual_ok=True, syncable=False,
            instance=_swarm_instance,
        ),
    }
    return specs


HOSTS: dict[str, HostSpec] = _build_specs()
HOST_NAMES: tuple[str, ...] = tuple(HOSTS)

# Derived views: the single source is HOSTS above.
MCP_WRITERS = {name: spec.mcp_writer for name, spec in HOSTS.items()}
HOOK_WRITERS = {
    name: spec.hook_writer for name, spec in HOSTS.items() if spec.hook_writer is not None
}
HOOK_DEFAULTS = {
    name: spec.default_hooks
    for name, spec in HOSTS.items()
    if spec.hook_writer is not None
}
SYNC_HOSTS: tuple[str, ...] = tuple(name for name, spec in HOSTS.items() if spec.syncable)
HOOK_HOSTS: tuple[str, ...] = tuple(
    name
    for name, spec in HOSTS.items()
    if spec.hook_writer is not None or spec.native_hooks
)


def get_host(name: str) -> HostSpec:
    try:
        return HOSTS[name]
    except KeyError as exc:
        raise ValueError(f"unsupported host: {name}") from exc
