# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host scan and auto-connect bookkeeping (F16 D16).

The scan grant records where the user allowed looking; :class:`PathView` is the
restricted traversal every host probe goes through — ``stat`` and directory
listing only, with no way to open a file. Probes identify instances by feature
paths; reading any content stays behind the sync grant (D15).

The orchestration below turns a scan report into connections: it reuses
``connect`` (D5) per instance, never overrides a user's ``disconnect`` and keeps
the runtime state of unattended runs in the same grant file.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.locks import file_lock

if TYPE_CHECKING:
    from jiuwen_memory_entry.local.install import Installation

GRANT_FILE = "scan.json"
GRANT_MODE = 0o600
GRANT_LOCK_TIMEOUT = 10.0
MANUAL_AFTER_FAILURES = 3
FAILED_OUTCOMES = frozenset({"incomplete", "aborted"})
EXCLUDED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"}
)
_INSTANCE_FIELDS = ("config_root", "profile", "edition", "scope", "project_root")


def _anchor(path: Path | str) -> Path:
    """Real path (symlinks resolved); falls back to the absolute path when it cannot."""
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(candidate))


@dataclass(frozen=True)
class ScanGrant:
    """Scan authorization plus the runtime state of auto-connect (``scan.json``)."""

    version: int = 1
    allowed: bool = False
    extra_dirs: tuple[str, ...] = ()      # 用户追加的项目目录，存 resolve() 后的真实路径
    depth: int = 4
    max_entries: int = 20000
    auto_connect: bool = True
    dismissed: tuple[str, ...] = ()       # 实例键，见 instance_key()
    pending_sync: tuple[str, ...] = ()    # 已自动接入、待授权同步的 install_id
    failures: dict[str, int] = field(default_factory=dict)  # 实例键 -> 连续自动接入失败次数
    manual: tuple[str, ...] = ()          # 连续失败 3 次后转为"需手动处理"的实例键
    included: tuple[str, ...] = ()        # scan include 恢复过的实例键，见 include_instance()

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra_dirs", _anchor_dirs(self.extra_dirs))
        object.__setattr__(self, "failures", dict(self.failures))

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "allowed": self.allowed,
            "extra_dirs": list(self.extra_dirs),
            "depth": self.depth,
            "max_entries": self.max_entries,
            "auto_connect": self.auto_connect,
            "dismissed": list(self.dismissed),
            "pending_sync": list(self.pending_sync),
            "failures": dict(self.failures),
            "manual": list(self.manual),
            "included": list(self.included),
        }


def _anchor_dirs(dirs: Iterable[str]) -> tuple[str, ...]:
    resolved: dict[str, None] = {}
    for item in dirs:
        text = str(item).strip()
        if text:
            resolved.setdefault(str(_anchor(text)), None)
    return tuple(resolved)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return ()
    return tuple(str(item) for item in value)


def _count(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _grant_from_dict(data: object) -> ScanGrant:
    if not isinstance(data, dict):
        return ScanGrant()
    defaults = ScanGrant()
    failures = data.get("failures")
    counts = failures if isinstance(failures, dict) else {}
    return ScanGrant(
        version=_count(data.get("version"), defaults.version),
        allowed=data["allowed"] if isinstance(data.get("allowed"), bool) else defaults.allowed,
        extra_dirs=_strings(data.get("extra_dirs")),
        depth=_count(data.get("depth"), defaults.depth),
        max_entries=_count(data.get("max_entries"), defaults.max_entries),
        auto_connect=(
            data["auto_connect"]
            if isinstance(data.get("auto_connect"), bool)
            else defaults.auto_connect
        ),
        dismissed=_strings(data.get("dismissed")),
        pending_sync=_strings(data.get("pending_sync")),
        failures={str(key): _count(value, 0) for key, value in counts.items()},
        manual=_strings(data.get("manual")),
        included=_strings(data.get("included")),
    )


def instance_key(host: str, instance: dict) -> str:
    """Stable key of one host instance: host plus the instance fields in fixed order."""
    return "|".join((host, *(str(instance.get(name) or "") for name in _INSTANCE_FIELDS)))


def _grant_path(home: MemoryHome) -> Path:
    return home.root / GRANT_FILE


_held_locks = threading.local()


@contextmanager
def scan_grant_lock(home: MemoryHome) -> Iterator[None]:
    """Exclusive ``scan.json`` lock; one thread may nest it for a read-modify-write."""
    path = _grant_path(home).with_name(GRANT_FILE + ".lock")
    key = os.path.normcase(str(path))
    depths = getattr(_held_locks, "depths", None)
    if depths is None:
        depths = _held_locks.depths = {}
    depth = depths.get(key, 0)
    depths[key] = depth + 1
    try:
        if depth:
            yield
        else:
            with file_lock(
                path,
                timeout=GRANT_LOCK_TIMEOUT,
                conflict_message="another process is updating the scan grant",
            ):
                yield
    finally:
        if depth:
            depths[key] = depth
        else:
            del depths[key]


def _read_grant(path: Path) -> ScanGrant:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ScanGrant()
    return _grant_from_dict(data)


def load_scan_grant(home: MemoryHome) -> ScanGrant:
    """The saved grant, or the default (not allowed) when there is none."""
    path = _grant_path(home)
    try:
        present = path.is_file()
    except OSError:
        present = False
    if not present:
        return ScanGrant()
    with scan_grant_lock(home):
        return _read_grant(path)


def save_scan_grant(home: MemoryHome, grant: ScanGrant) -> None:
    """Atomically write the grant with owner-only permission."""
    path = _grant_path(home)
    home.root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(grant.as_dict(), indent=2) + "\n"
    with scan_grant_lock(home):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, GRANT_MODE)
        os.replace(temporary, path)


def change_scan_grant(grant: ScanGrant, **changes) -> ScanGrant:
    """Apply an authorization change and advance the version.

    Runtime state (``pending_sync``/``failures``/``manual``) is updated with
    :func:`dataclasses.replace` instead, which leaves the version alone.
    """
    updated = replace(grant, **changes)
    object.__setattr__(updated, "version", grant.version + 1)
    return updated


class PathView:
    """Restricted traversal for host probes: only ``stat`` and directory listing.

    Every path is resolved first; anything that lands outside the roots —
    including through a symlink — is refused and reported in :attr:`skipped`.
    """

    def __init__(
        self,
        roots: Sequence[Path | str],
        *,
        depth: int,
        max_entries: int,
        extra_roots: Sequence[Path | str] = (),
    ) -> None:
        """``roots`` are everywhere the view may look; ``extra_roots`` are added to them.

        ``extra_roots`` are the user-added project directories, the ones walked
        for project-level markers.
        """
        self._depth = _count(depth, 0)
        self._max_entries = _count(max_entries, 0)
        self._notes: set[str] = set()
        self.truncated: list[str] = []
        self.skipped: list[str] = []
        self._extra_roots = tuple(_anchor(path) for path in extra_roots)
        self._roots = _unique((*map(_anchor, roots), *self._extra_roots))

    @property
    def extra_roots(self) -> tuple[Path, ...]:
        """User-added project directories, always part of the roots."""
        return self._extra_roots

    def within_roots(self, path: Path | str) -> bool:
        """Whether ``path`` resolves inside the roots. Nothing is recorded."""
        return self._admit(path, report=False) is not None

    def is_dir(self, path: Path | str) -> bool:
        real = self._admit(path)
        return real is not None and self._directory(real, path)

    def is_file(self, path: Path | str) -> bool:
        real = self._admit(path)
        if real is None:
            return False
        try:
            return real.is_file()
        except OSError as exc:
            self._skip(f"{path}: {exc}")
            return False

    def list_dir(self, path: Path | str) -> list[Path]:
        """Sorted children of a directory inside the roots; empty when refused."""
        real = self._admit(path)
        if real is None:
            return []
        try:
            return sorted(real.iterdir())
        except OSError as exc:
            self._skip(f"{path}: {exc}")
            return []

    def walk_for(self, root: Path | str, marker: str) -> list[Path]:
        """Directories from ``root`` down to the depth limit holding ``marker``.

        Excluded directory names are never entered. Hitting the entry or depth
        limit stops the walk and appends one note per root to :attr:`truncated`.
        """
        start = self._admit(root)
        if start is None:
            return []
        matches: list[Path] = []
        seen: set[Path] = {start}
        level: list[Path] = [start]
        visited = 0
        for depth in range(self._depth + 1):
            deeper: list[Path] = []
            for directory in level:
                children = self.list_dir(directory)
                visited += len(children)
                if visited > self._max_entries:
                    self._truncated(f"{start}: entry limit reached")
                    return matches
                subdirectories: list[Path] = []
                for child in children:
                    if child.name in EXCLUDED_DIRS:
                        continue
                    real = self._admit(child)
                    if real is None or real in seen or not self._directory(real, child):
                        continue
                    if child.name == marker:
                        matches.append(directory)
                    subdirectories.append(real)
                if depth == self._depth:
                    if subdirectories:
                        self._truncated(f"{start}: depth limit reached")
                else:
                    deeper.extend(subdirectories)
                    seen.update(subdirectories)
            level = deeper
            if not level:
                break
        return matches

    def _admit(self, path: Path | str, *, report: bool = True) -> Path | None:
        reason = "outside scan roots"
        try:
            real = Path(path).expanduser().resolve()
            if any(real.is_relative_to(root) for root in self._roots):
                return real
        except (OSError, RuntimeError) as exc:
            reason = str(exc)
        if report:
            self._skip(f"{path}: {reason}")
        return None

    def _directory(self, real: Path, display: Path | str) -> bool:
        try:
            return real.is_dir()
        except OSError as exc:
            self._skip(f"{display}: {exc}")
            return False

    def _truncated(self, message: str) -> None:
        self._record(self.truncated, message)

    def _skip(self, message: str) -> None:
        self._record(self.skipped, message)

    def _record(self, notes: list[str], message: str) -> None:
        if message not in self._notes:
            self._notes.add(message)
            notes.append(message)


def _unique(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(paths))


@dataclass(frozen=True)
class ProbeResult:
    """One host instance recognized by its feature paths."""

    host: str
    instance: dict[str, str]
    evidence: tuple[str, ...]
    memory_roots: tuple[str, ...]
    importable: tuple[str, ...]
    state: Literal["found", "suspected", "unavailable"]
    reason: str = ""


@dataclass
class ScanReport:
    results: list[ProbeResult]
    truncated: list[str]
    skipped: list[str]


def default_locations() -> list[Path]:
    """Default host locations as configured, whether or not they exist.

    Only paths are built here, nothing is stat'ed: the consent prompt lists them
    before the user has allowed looking at anything.
    """
    from jiuwen_memory_entry.local.hosts.codex import codex_home
    from jiuwen_memory_entry.local.hosts.dsh import dsh_home
    from jiuwen_memory_entry.local.hosts.hermes import hermes_home
    from jiuwen_memory_entry.local.hosts.registry import workbuddy_homes

    roots = [codex_home(), dsh_home(), hermes_home()]
    roots.extend(home for _edition, home in workbuddy_homes())
    return list(dict.fromkeys(path.expanduser() for path in roots))


def default_roots() -> list[Path]:
    """Default host locations (Codex, DSH, Hermes, both WorkBuddy editions) that exist."""
    roots: list[Path] = []
    for candidate in default_locations():
        root = _anchor(candidate)
        if root in roots or not _is_directory(root):
            continue
        roots.append(root)
    return roots


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def probe_all(grant: ScanGrant) -> ScanReport:
    """Run every registered host probe inside one bounded view of the granted locations."""
    from jiuwen_memory_entry.local.hosts.registry import HOSTS

    if not grant.allowed:
        return ScanReport([], [], [])
    extra = [Path(item) for item in grant.extra_dirs]
    view = PathView(
        [*default_roots(), *extra],
        extra_roots=extra,
        depth=grant.depth,
        max_entries=grant.max_entries,
    )
    results: list[ProbeResult] = []
    for spec in HOSTS.values():
        if spec.probe is not None:
            results.extend(spec.probe(view))
    return ScanReport(results, list(view.truncated), list(view.skipped))


@dataclass
class ConnectOutcome:
    """What became of one scanned instance in a batch connect.

    ``state`` is ``complete`` / ``incomplete`` / ``aborted`` as reported by
    ``install.connect``. ``skipped`` (already connected), ``dismissed`` (the user
    ignored it) and ``unavailable`` (found, but it cannot be connected) mean
    nothing was attempted and ``install_id`` is empty.
    """

    key: str
    host: str
    install_id: str
    state: str
    message: str = ""


@contextmanager
def _writable_grant_lock(home: MemoryHome) -> Iterator[None]:
    """The grant lock, with the data directory made sure to exist first."""
    home.root.mkdir(parents=True, exist_ok=True)
    with scan_grant_lock(home):
        yield


def _installs(home: MemoryHome) -> list[Installation]:
    """Every install record, connected or not.

    They tell which instances the user already ruled on, e.g. by disconnecting.
    """
    from jiuwen_memory_entry.local.install import _load_install

    if not home.installs_dir.is_dir():
        return []
    found: list[Installation] = []
    for record in sorted(home.installs_dir.glob("*/install.json")):
        install = _load_install(home, record.parent.name)
        if install is not None:
            found.append(install)
    return found


def _same_instance(install: Installation, result: ProbeResult) -> bool:
    """Whether an install record is the very instance the probe found.

    Records written before instances were recorded compare by their top-level
    profile and edition; an empty field on the record's side matches anything.
    """
    from jiuwen_memory_entry.local.install import InstanceRef

    if install.host != result.host:
        return False
    known = install.instance or InstanceRef(
        install.host, profile=install.profile, edition=install.edition
    )
    found = InstanceRef(
        result.host, **{name: result.instance.get(name, "") for name in _INSTANCE_FIELDS}
    )
    return known.matches(found)


def _blocking_reason(
    installs: Sequence[Installation], result: ProbeResult, grant: ScanGrant
) -> tuple[str, str, str]:
    """Why ``result`` must not be connected: ``(reason, install id, message)``.

    The reason is empty when it may be connected; otherwise ``ignored``,
    ``connected`` or ``disconnected``. A connected record wins over a
    disconnected one for the same instance, and a disconnected one only counts
    for an instance the user has not included again since.
    """
    from jiuwen_memory_entry.local.install import STATE_CONNECTED, STATE_DISCONNECTED

    key = instance_key(result.host, result.instance)
    if key in grant.dismissed or key in grant.manual:
        return "ignored", "", "ignored by the user"
    matching = [install for install in installs if _same_instance(install, result)]
    for install in matching:
        if install.state == STATE_CONNECTED:
            return "connected", install.install_id, "already connected"
    if key not in grant.included:
        for install in matching:
            if install.state == STATE_DISCONNECTED:
                return "disconnected", "", "disconnected by the user earlier"
    return "", "", ""


def candidates(
    home: MemoryHome, report: ScanReport, grant: ScanGrant
) -> list[ProbeResult]:
    """Found instances that may be connected now, in report order.

    Left out: everything not ``found``; instances in ``dismissed`` or ``manual``;
    instances already connected; and instances whose install the user has
    disconnected — those are remembered as dismissed too, so a later scan never
    overrides that decision.
    """
    installs = _installs(home)
    chosen: list[ProbeResult] = []
    lapsed: list[str] = []
    for result in report.results:
        if result.state != "found":
            continue
        reason, _install_id, _message = _blocking_reason(installs, result, grant)
        if reason == "disconnected":
            lapsed.append(instance_key(result.host, result.instance))
        elif not reason:
            chosen.append(result)
    _dismiss(home, lapsed)
    return chosen


def _connect_one(
    home: MemoryHome,
    result: ProbeResult,
    installs: Sequence[Installation],
    grant: ScanGrant,
    confirm: Callable[[str], bool] | None,
) -> ConnectOutcome:
    """Connect one instance unless the probe or the user's earlier decisions rule it out.

    A disconnect that lands while the connection is being made still wins: the
    fresh connection is undone again.
    """
    from jiuwen_memory_entry.local.install import connect

    key = instance_key(result.host, result.instance)
    if result.state != "found":
        state = "unavailable" if result.state == "unavailable" else "skipped"
        return ConnectOutcome(key, result.host, "", state, result.reason)
    reason, install_id, message = _blocking_reason(installs, result, grant)
    if reason:
        return ConnectOutcome(
            key, result.host, install_id,
            "skipped" if reason == "connected" else "dismissed", message,
        )
    connected = connect(
        home,
        result.host,
        profile=result.instance.get("profile", ""),
        edition=result.instance.get("edition", ""),
        scope=result.instance.get("scope", ""),
        project_root=result.instance.get("project_root", ""),
        hooks=None,
        capture=None,
        confirm=confirm,
    )
    outcome = ConnectOutcome(
        key, result.host, connected.install_id, connected.state, connected.message
    )
    if connected.state == "complete" and key in _dismissed_keys(home):
        return _disconnect_again(home, outcome)
    return outcome


def _dismissed_keys(home: MemoryHome) -> frozenset[str]:
    with _writable_grant_lock(home):
        return frozenset(load_scan_grant(home).dismissed)


def _disconnect_again(home: MemoryHome, outcome: ConnectOutcome) -> ConnectOutcome:
    """Undo a connection the user dismissed while it was being made."""
    from jiuwen_memory_entry.local.install import disconnect

    undone = "the user disconnected this instance while it was being connected"
    try:
        disconnect(home, outcome.install_id)
    except Exception as exc:
        return replace(
            outcome,
            state="incomplete",
            message=(
                f"{outcome.message}; {undone}, but disconnecting it again failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    return ConnectOutcome(outcome.key, outcome.host, outcome.install_id, "dismissed", undone)


def _waits_for_sync(result: ProbeResult | None) -> bool:
    """Whether a connect result has memory that a later sync will import."""
    if result is None:
        return False
    return bool(result.memory_roots and result.importable)


def _record_outcomes(
    home: MemoryHome,
    results: Sequence[ProbeResult],
    outcomes: Sequence[ConnectOutcome],
    *,
    background: bool,
) -> None:
    """Fold the outcomes into the grant's runtime state; the version does not move.

    A connected instance that has memory to import waits for sync authorization.
    In an unattended run every failure counts; three in a row move the instance
    to ``manual`` and reset its count, a success resets it as well.
    """
    offered = {instance_key(result.host, result.instance): result for result in results}
    with _writable_grant_lock(home):
        grant = load_scan_grant(home)
        pending = list(grant.pending_sync)
        failures = dict(grant.failures)
        manual = list(grant.manual)
        for outcome in outcomes:
            if outcome.state == "complete":
                failures.pop(outcome.key, None)
                result = offered.get(outcome.key)
                if outcome.install_id and _waits_for_sync(result) and (
                    outcome.install_id not in pending
                ):
                    pending.append(outcome.install_id)
            elif background and outcome.state in FAILED_OUTCOMES:
                count = failures.get(outcome.key, 0) + 1
                if count >= MANUAL_AFTER_FAILURES:
                    failures.pop(outcome.key, None)
                    if outcome.key not in manual:
                        manual.append(outcome.key)
                else:
                    failures[outcome.key] = count
        updated = replace(
            grant, pending_sync=tuple(pending), failures=failures, manual=tuple(manual)
        )
        if updated != grant:
            save_scan_grant(home, updated)


def connect_results(
    home: MemoryHome,
    results: Sequence[ProbeResult],
    *,
    confirm: Callable[[str], bool] | None = None,
    background: bool = False,
) -> list[ConnectOutcome]:
    """Connect scanned instances one by one; one failure never stops the rest.

    Hooks and capture follow each host's defaults (``hooks=None``). The grant, the
    ignored list and the install records are read right before connecting, so a
    decision the user made while the scan was running wins: without the scan
    grant nothing is connected at all.
    """
    installs = _installs(home)
    with _writable_grant_lock(home):
        grant = load_scan_grant(home)
    outcomes: list[ConnectOutcome] = []
    for result in results:
        key = instance_key(result.host, result.instance)
        if not grant.allowed:
            outcomes.append(
                ConnectOutcome(key, result.host, "", "skipped", "scan is not allowed")
            )
            continue
        try:
            outcomes.append(_connect_one(home, result, installs, grant, confirm))
        except Exception as exc:
            outcomes.append(
                ConnectOutcome(key, result.host, "", "incomplete", f"{type(exc).__name__}: {exc}")
            )
    _record_outcomes(home, results, outcomes, background=background)
    return outcomes


def _dismiss(home: MemoryHome, keys: Iterable[str]) -> None:
    """Remember instance keys as ignored; keys already ignored change nothing."""
    with _writable_grant_lock(home):
        grant = load_scan_grant(home)
        added = tuple(key for key in keys if key not in grant.dismissed)
        if added:
            save_scan_grant(
                home,
                change_scan_grant(
                    grant,
                    dismissed=(*grant.dismissed, *added),
                    included=tuple(item for item in grant.included if item not in added),
                ),
            )


def dismiss_instance(home: MemoryHome, host: str, instance: dict) -> None:
    """Ignore one instance from now on; only ``scan include`` brings it back."""
    _dismiss(home, (instance_key(host, instance),))


def include_instance(home: MemoryHome, key: str) -> None:
    """Make an ignored instance — dismissed or needing manual action — eligible again.

    The key is remembered as included, so an install the user disconnected
    earlier does not count as ignoring the instance on the next scan.
    """
    with _writable_grant_lock(home):
        grant = load_scan_grant(home)
        if key not in grant.dismissed and key not in grant.manual:
            return
        included = grant.included if key in grant.included else (*grant.included, key)
        save_scan_grant(
            home,
            change_scan_grant(
                grant,
                dismissed=tuple(item for item in grant.dismissed if item != key),
                manual=tuple(item for item in grant.manual if item != key),
                included=included,
            ),
        )


def resolve_pending_sync(home: MemoryHome, install_id: str) -> None:
    """Stop reminding about an install once its sync has been granted."""
    with _writable_grant_lock(home):
        grant = load_scan_grant(home)
        if install_id in grant.pending_sync:
            save_scan_grant(
                home,
                replace(
                    grant,
                    pending_sync=tuple(item for item in grant.pending_sync if item != install_id),
                ),
            )
