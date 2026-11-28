# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host registry: adding a host means adding one entry, not editing install/CLI."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from jiuwen_memory_entry.local.hosts import (
    CAPABILITIES,
    HookWriter,
    HostCapabilities,
    HostWriter,
    TakeoverWriter,
)
from jiuwen_memory_entry.local.scan import PathView, ProbeResult

_CODEX_MARKERS = ("config.toml", "sessions", "memories")
_HERMES_MARKERS = ("memories", "config.yaml", "state.db")
_HERMES_PROFILE_MARKERS = ("memories", "config.yaml")
_WORKBUDDY_MARKERS = ("USER.md", "SOUL.md", "mcp.json", ".mcp.json")
_LONG_TERM_TYPES = {
    "codex": ("fact",),
    "hermes": ("fact", "profile"),
    "workbuddy": ("profile", "fact", "daily_log"),
}
_NO_USER_INSTANCE = {
    "config_root": "",
    "edition": "",
    "scope": "user",
    "project_root": "",
}


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
    probe: Callable[[PathView], list[ProbeResult]] | None = None
    takeover: Callable[[], TakeoverWriter] | None = None


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

    layout = detect_workbuddy(project_root or None, edition)
    return {
        "config_root": str((layout.home or Path.home()).resolve()),
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


def _importable(host: str) -> tuple[str, ...]:
    """Memory types a host may import: only capabilities declared ``supported``."""
    capabilities = CAPABILITIES[host]
    types = _LONG_TERM_TYPES.get(host, ()) if capabilities.memory_import == "supported" else ()
    if capabilities.session_import == "supported":
        types = (*types, "session_turn")
    return types


def _visible_dir(view: PathView, home: Path) -> Path | None:
    """``home`` when it is a directory the view may look at, else ``None``."""
    if not view.within_roots(home) or not view.is_dir(home):
        return None
    return home.expanduser().resolve()


def _present(view: PathView, base: Path, names: tuple[str, ...]) -> tuple[Path, ...]:
    """Feature paths under ``base`` that exist as a file or a directory."""
    found: list[Path] = []
    for entry in names:
        path = base / entry
        if view.is_file(path) or view.is_dir(path):
            found.append(path.resolve())
    return tuple(found)


def _memories_root(view: PathView, base: Path) -> tuple[str, ...]:
    """``base/memories`` as a Hermes instance's own source root; empty when absent."""
    memories = base / "memories"
    return (str(memories.resolve()),) if view.is_dir(memories) else ()


def _recognized(
    host: str,
    instance: dict,
    root: Path,
    importable: tuple[str, ...],
    found: Sequence[Path] = (),
    memory_roots: Sequence[str] | None = None,
) -> ProbeResult:
    """``found`` when a feature path exists under ``root``, otherwise ``suspected``.

    ``memory_roots`` are the source roots offered for sync; ``root`` itself when unsaid.
    """
    return ProbeResult(
        host=host,
        instance=instance,
        evidence=tuple(str(path) for path in found) or (str(root),),
        memory_roots=tuple(memory_roots) if memory_roots is not None else (str(root),),
        importable=importable,
        state="found" if found else "suspected",
    )


def _probe_codex(view: PathView) -> list[ProbeResult]:
    from jiuwen_memory_entry.local.hosts.codex import codex_home

    home = codex_home()
    root = _visible_dir(view, home)
    if root is None:
        return []
    return [
        _recognized(
            "codex",
            _codex_instance(),
            root,
            _importable("codex"),
            _present(view, home, _CODEX_MARKERS),
        )
    ]


def _probe_dsh(view: PathView) -> list[ProbeResult]:
    from jiuwen_memory_entry.local.hosts.dsh import PROFILES, dsh_home

    home = dsh_home()
    root = _visible_dir(view, home)
    if root is None:
        return []
    importable = _importable("dsh")
    directories = [
        directory
        for directory in (home / "profiles" / profile for profile in PROFILES)
        if view.is_dir(directory)
    ]
    if not directories:
        return [_recognized("dsh", _dsh_instance(), root, importable)]
    missing = shutil.which("dsh") is None
    owner = directories[0].name
    results: list[ProbeResult] = []
    for index, directory in enumerate(directories):
        later = index > 0
        reasons = ["dsh command not found"] if missing else []
        if later:
            reasons.append(f"sessions are shared with profile {owner}")
        results.append(
            ProbeResult(
                host="dsh",
                instance=_dsh_instance(profile=directory.name),
                evidence=(str(directory.resolve()),),
                memory_roots=() if later else (str(root),),
                importable=() if later else importable,
                state="unavailable" if missing else "found",
                reason="; ".join(reasons),
            )
        )
    return results


def _probe_hermes(view: PathView) -> list[ProbeResult]:
    from jiuwen_memory_entry.local.hosts.hermes import hermes_home

    home = hermes_home()
    root = _visible_dir(view, home)
    if root is None:
        return []
    importable = _importable("hermes")
    results = [
        _recognized(
            "hermes",
            _hermes_instance(),
            root,
            importable,
            _present(view, home, _HERMES_MARKERS),
            _memories_root(view, home),
        )
    ]
    profiles = home / "profiles"
    if view.is_dir(profiles):
        for directory in view.list_dir(profiles):
            if not view.is_dir(directory):
                continue
            found = _present(view, directory, _HERMES_PROFILE_MARKERS)
            if found:
                results.append(
                    ProbeResult(
                        host="hermes",
                        instance=_hermes_instance(profile=directory.name),
                        evidence=tuple(str(path) for path in found),
                        memory_roots=_memories_root(view, directory),
                        importable=importable,
                        state="found",
                    )
                )
    return results


def workbuddy_homes() -> tuple[tuple[str, Path], ...]:
    """User-level home of each WorkBuddy edition, from the env or the user directory."""
    community = Path(
        os.environ.get("JIUWEN_WORKBUDDY_HOME", Path.home() / ".workbuddy")
    ).expanduser()
    enterprise = Path(
        os.environ.get("JIUWEN_CODEBUDDY_HOME", Path.home() / ".codebuddy")
    ).expanduser()
    return (("community", community), ("enterprise", enterprise))


def _probe_workbuddy(view: PathView) -> list[ProbeResult]:
    importable = _importable("workbuddy")
    results: list[ProbeResult] = []
    for edition, home in workbuddy_homes():
        root = _visible_dir(view, home)
        if root is None:
            continue
        instance = {
            "config_root": str(root),
            "edition": edition,
            "scope": "user",
            "project_root": "",
        }
        results.append(
            _recognized(
                "workbuddy",
                instance,
                root,
                importable,
                _present(view, home, _WORKBUDDY_MARKERS),
            )
        )
    projects: list[Path] = []
    for extra in view.extra_roots:
        for directory in view.walk_for(extra, ".workbuddy"):
            if view.is_dir(directory / ".workbuddy" / "memory"):
                projects.append(directory)
    if not projects:
        return results
    roots = tuple(str(directory) for directory in projects)
    if not results:
        return [
            ProbeResult(
                host="workbuddy",
                instance=dict(_NO_USER_INSTANCE),
                evidence=tuple(str(Path(root) / ".workbuddy" / "memory") for root in roots),
                memory_roots=roots,
                importable=importable,
                state="suspected",
                reason="project memory found but no user-level WorkBuddy",
            )
        ]
    owner, *others = results
    return [replace(owner, memory_roots=(*owner.memory_roots, *roots)), *others]


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
            default_hooks=True, instance=_codex_instance, probe=_probe_codex,
        ),
        "dsh": HostSpec(
            "dsh", CAPABILITIES["dsh"], DshMcpWriter, DshHookWriter,
            instance=_dsh_instance, upgrade_instance=_dsh_upgrade, probe=_probe_dsh,
        ),
        "hermes": HostSpec(
            "hermes", CAPABILITIES["hermes"], HermesMcpWriter, HermesHookWriter,
            instance=_hermes_instance, probe=_probe_hermes,
        ),
        "workbuddy": HostSpec(
            "workbuddy", CAPABILITIES["workbuddy"], WorkBuddyMcpWriter,
            WorkBuddyHookWriter, instance=_workbuddy_instance, probe=_probe_workbuddy,
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
