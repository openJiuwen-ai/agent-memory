"""Host scan (F16 D16): path-only probes behind a bounded view, plus the scan grant."""

from __future__ import annotations

import builtins
import os
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from dataclasses import fields, replace
from itertools import combinations
from pathlib import Path

import pytest

from jiuwen_memory_entry.local.home import MemoryHome
from jiuwen_memory_entry.local.hosts.registry import HOSTS, HostSpec
from jiuwen_memory_entry.local.scan import (
    PathView,
    ScanGrant,
    ScanReport,
    change_scan_grant,
    default_roots,
    instance_key,
    load_scan_grant,
    probe_all,
    save_scan_grant,
    scan_grant_lock,
)

pytestmark = pytest.mark.unit

_HOST_HOMES = (
    ("JIUWEN_CODEX_HOME", "codex"),
    ("JIUWEN_DSH_HOME", "dsh"),
    ("JIUWEN_HERMES_HOME", "hermes"),
    ("JIUWEN_WORKBUDDY_HOME", "workbuddy"),
    ("JIUWEN_CODEBUDDY_HOME", "codebuddy"),
)
_ALLOWED = ScanGrant(allowed=True)
_HOLD_LOCK = """
import sys
import time
from pathlib import Path
from jiuwen_memory_entry.local.locks import file_lock
with file_lock(Path(sys.argv[1]), timeout=0):
    print("locked", flush=True)
    time.sleep(1.0)
"""


class _Homes:
    """Fake user directory: every host home lives under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.codex = root / "codex"
        self.dsh = root / "dsh"
        self.hermes = root / "hermes"
        self.workbuddy = root / "workbuddy"
        self.codebuddy = root / "codebuddy"

    @property
    def projects(self) -> Path:
        return self.root / "projects"


@pytest.fixture
def homes(tmp_path, monkeypatch) -> _Homes:
    fake = _Homes(tmp_path)
    for variable, attribute in _HOST_HOMES:
        monkeypatch.setenv(variable, str(getattr(fake, attribute)))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda command: "/usr/bin/dsh")
    return fake


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder\n", encoding="utf-8")
    return path


def _project_memory(project: Path, name: str = "2026-01-01.md") -> Path:
    return _touch(project / ".workbuddy" / "memory" / name)


def _installed(homes: _Homes) -> None:
    _touch(homes.codex / "config.toml")
    (homes.codex / "sessions").mkdir(parents=True)
    (homes.dsh / "profiles" / "desktop").mkdir(parents=True)
    (homes.dsh / "profiles" / "dsh-tui").mkdir(parents=True)
    _touch(homes.hermes / "config.yaml")
    (homes.hermes / "memories").mkdir(parents=True)
    _touch(homes.workbuddy / "USER.md")
    _touch(homes.codebuddy / ".mcp.json")


def _host(report: ScanReport, host: str) -> list:
    return [result for result in report.results if result.host == host]


def _project_roots(result, projects: Path) -> set[Path]:
    base = projects.resolve()
    return {Path(root).resolve() for root in result.memory_roots if Path(root).is_relative_to(base)}


def _tree(root: Path) -> list[str]:
    base = root.resolve()
    entries: list[str] = []
    for directory, names, files in os.walk(root):
        names.sort()
        entries.append(str(Path(directory).resolve().relative_to(base)) + "/")
        entries.extend(
            str(Path(directory, name).resolve().relative_to(base)) for name in sorted(files)
        )
    return entries


def test_probe_opens_no_files(homes, monkeypatch):
    _installed(homes)

    def refuse(*_args, **_kwargs):
        raise AssertionError("scan opened a file")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    report = probe_all(_ALLOWED)
    found = {result.host for result in report.results if result.state == "found"}
    assert found == {"codex", "dsh", "hermes", "workbuddy"}, (
        f"hosts not recognized: {sorted(found)}"
    )
    assert report.skipped == [], f"clean directories were skipped: {report.skipped}"
    assert report.truncated == [], f"scan was truncated with no limit hit: {report.truncated}"


def test_probe_creates_no_files(homes):
    _installed(homes)
    (homes.projects / "svc" / ".workbuddy" / "memory").mkdir(parents=True)
    before = _tree(homes.root)
    probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),)))
    assert _tree(homes.root) == before, "scan changed files under the fake user directory"


def test_registry_wires_probes_for_four_hosts():
    probed = {name for name, spec in HOSTS.items() if spec.probe is not None}
    assert probed == {"codex", "dsh", "hermes", "workbuddy"}, (
        f"unexpected probe wiring: {sorted(probed)}"
    )
    assert HOSTS["jiuwenswarm"].probe is None, "JiuwenSwarm is native and must not be probed"
    assert "takeover_key" not in {field.name for field in fields(HostSpec)}, (
        "takeover_key was dropped; takeover is declared by a callable field instead"
    )


def test_hermes_profiles_are_separate_instances(homes):
    _touch(homes.hermes / "config.yaml")
    profiles = homes.hermes / "profiles"
    (profiles / "work" / "memories").mkdir(parents=True)
    _touch(profiles / "life" / "config.yaml")
    (profiles / "empty").mkdir(parents=True)
    results = _host(probe_all(_ALLOWED), "hermes")
    by_profile = {result.instance["profile"]: result for result in results}
    assert set(by_profile) == {"default", "work", "life"}, (
        f"unexpected profiles: {sorted(by_profile)}"
    )
    assert {result.state for result in results} == {"found"}, (
        "profile directories with markers must be found"
    )
    assert all(
        result.instance["config_root"] == str(homes.hermes.resolve()) for result in results
    ), "config_root must be the resolved Hermes home"
    work, life, default = (by_profile[name] for name in ("work", "life", "default"))
    assert work.memory_roots == (str((profiles / "work" / "memories").resolve()),), (
        "a profile's only source root is its own memories directory"
    )
    assert work.evidence == (str((profiles / "work" / "memories").resolve()),)
    assert life.evidence == (str((profiles / "life" / "config.yaml").resolve()),)
    assert life.memory_roots == (), "an instance without memories/ offers nothing for sync"
    assert default.memory_roots == (), "the Hermes home itself is never a source root"
    assert len({instance_key(result.host, result.instance) for result in results}) == 3, (
        "profiles must map to distinct instance keys"
    )


def test_hermes_memory_roots_do_not_overlap(homes):
    profiles = homes.hermes / "profiles"
    _touch(homes.hermes / "memories" / "MEMORY.md")
    _touch(profiles / "work" / "memories" / "USER.md")
    _touch(profiles / "life" / "memories" / "MEMORY.md")
    results = _host(probe_all(_ALLOWED), "hermes")
    assert {result.state for result in results} == {"found"}, (
        "every instance holds a memories directory with a marker"
    )
    roots = {result.instance["profile"]: result.memory_roots for result in results}
    assert roots == {
        "default": (str((homes.hermes / "memories").resolve()),),
        "work": (str((profiles / "work" / "memories").resolve()),),
        "life": (str((profiles / "life" / "memories").resolve()),),
    }, f"each instance must offer exactly its own memories directory: {roots}"
    offered = [Path(root) for pair in roots.values() for root in pair]
    for one, two in combinations(offered, 2):
        assert not one.is_relative_to(two) and not two.is_relative_to(one), (
            f"source roots overlap, the same file would be imported twice: {one} and {two}"
        )


def test_dsh_profiles_are_separate_instances(homes):
    (homes.dsh / "profiles" / "desktop").mkdir(parents=True)
    (homes.dsh / "profiles" / "dsh-tui").mkdir(parents=True)
    results = _host(probe_all(_ALLOWED), "dsh")
    assert [result.instance["profile"] for result in results] == ["desktop", "dsh-tui"]
    assert {result.state for result in results} == {"found"}, "profile directories must be found"
    assert all(
        result.instance["config_root"] == str(homes.dsh.resolve()) for result in results
    ), "config_root must be the resolved DSH home"
    assert results[0].evidence == (str((homes.dsh / "profiles" / "desktop").resolve()),)
    assert len({instance_key(result.host, result.instance) for result in results}) == 2, (
        "profiles must map to distinct instance keys"
    )


def test_dsh_sessions_belong_to_one_instance(homes):
    (homes.dsh / "profiles" / "dsh-tui").mkdir(parents=True)
    (homes.dsh / "profiles" / "desktop").mkdir(parents=True)
    results = _host(probe_all(_ALLOWED), "dsh")
    assert [result.instance["profile"] for result in results] == ["desktop", "dsh-tui"], (
        "the owning profile follows PROFILES order, not the order directories appear in"
    )
    assert {result.state for result in results} == {"found"}, (
        "sharing sessions changes no state"
    )
    owner, later = results
    assert owner.memory_roots == (str(homes.dsh.resolve()),), (
        "the first profile found carries the shared sessions directory"
    )
    assert owner.importable == ("session_turn",), "the owning profile imports the sessions"
    assert owner.reason == "", "the owning profile needs no explanation"
    assert later.memory_roots == (), "sessions must not be offered through a second instance"
    assert later.importable == (), "a second instance would import the same sessions twice"
    assert later.reason == "sessions are shared with profile desktop"


def test_dsh_shared_sessions_note_keeps_unavailable_reason(homes, monkeypatch):
    (homes.dsh / "profiles" / "desktop").mkdir(parents=True)
    (homes.dsh / "profiles" / "dsh-tui").mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda command: None)
    owner, later = _host(probe_all(_ALLOWED), "dsh")
    assert {result.state for result in (owner, later)} == {"unavailable"}, (
        "a missing dsh command leaves every profile unavailable"
    )
    assert owner.reason == "dsh command not found"
    assert later.reason == "dsh command not found; sessions are shared with profile desktop", (
        "the sharing note must not hide why the instance is unavailable"
    )


def test_workbuddy_editions_are_separate_instances(homes):
    _touch(homes.workbuddy / "USER.md")
    _touch(homes.codebuddy / ".mcp.json")
    results = _host(probe_all(_ALLOWED), "workbuddy")
    assert [result.instance["edition"] for result in results] == ["community", "enterprise"]
    assert {result.state for result in results} == {"found"}, "homes with markers must be found"
    community, enterprise = results
    assert community.instance["config_root"] == str(homes.workbuddy.resolve())
    assert enterprise.instance["config_root"] == str(homes.codebuddy.resolve())
    assert {result.instance["scope"] for result in results} == {"user"}, (
        "editions default to user scope"
    )
    assert community.evidence == (str((homes.workbuddy / "USER.md").resolve()),)
    assert enterprise.evidence == (str((homes.codebuddy / ".mcp.json").resolve()),)
    assert community.memory_roots == (str(homes.workbuddy.resolve()),)
    assert enterprise.memory_roots == (str(homes.codebuddy.resolve()),)
    assert len({instance_key(result.host, result.instance) for result in results}) == 2, (
        "editions must map to distinct instance keys"
    )


def test_directory_without_markers_is_suspected(homes):
    for _variable, attribute in _HOST_HOMES:
        getattr(homes, attribute).mkdir()
    report = probe_all(_ALLOWED)
    assert Counter(result.host for result in report.results) == {
        "codex": 1,
        "dsh": 1,
        "hermes": 1,
        "workbuddy": 2,
    }, f"one suspected result per existing home expected: {report.results}"
    assert {result.state for result in report.results} == {"suspected"}, (
        "a bare directory is not evidence enough to connect"
    )
    assert all(not result.reason for result in report.results), (
        "suspected results carry no failure reason"
    )
    (codex,) = _host(report, "codex")
    assert codex.evidence == (str(homes.codex.resolve()),), "evidence is the bare directory itself"
    (dsh,) = _host(report, "dsh")
    assert dsh.instance["profile"] == "default"


def test_dsh_without_command_is_unavailable(homes, monkeypatch):
    (homes.dsh / "profiles" / "desktop").mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda command: None)
    (result,) = _host(probe_all(_ALLOWED), "dsh")
    assert result.state == "unavailable", f"expected unavailable, got {result.state}"
    assert result.reason == "dsh command not found"
    assert result.instance["profile"] == "desktop"


def test_symlink_escaping_root_is_not_followed(homes):
    _touch(homes.workbuddy / "USER.md")
    homes.projects.mkdir()
    outside = homes.root / "outside"
    (outside / ".workbuddy" / "memory").mkdir(parents=True)
    os.symlink(outside, homes.projects / "escape")
    report = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),)))
    roots = [Path(root) for result in report.results for root in result.memory_roots]
    assert not any(root.is_relative_to(outside.resolve()) for root in roots), (
        f"symlink target outside the scanned root was followed: {roots}"
    )
    assert any("outside scan roots" in note for note in report.skipped), (
        f"refused symlink was not reported: {report.skipped}"
    )
    assert report.skipped == [f"{homes.projects.resolve() / 'escape'}: outside scan roots"], (
        "the escaping symlink itself must be the refused path"
    )


def test_walk_skips_excluded_dirs_and_reports_depth(homes):
    _touch(homes.workbuddy / "USER.md")
    for relative in ("node_modules/x", "a/b", "ok"):
        (homes.projects / relative / ".workbuddy" / "memory").mkdir(parents=True)
    _project_memory(homes.projects / "ok")

    shallow = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),), depth=1))
    (community,) = _host(shallow, "workbuddy")
    assert _project_roots(community, homes.projects) == {(homes.projects / "ok").resolve()}, (
        "excluded and too-deep project memories must not be reported"
    )
    assert shallow.truncated == [f"{homes.projects.resolve()}: depth limit reached"], (
        f"depth limit must be reported once per root: {shallow.truncated}"
    )

    deep = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),), depth=4))
    (community,) = _host(deep, "workbuddy")
    assert _project_roots(community, homes.projects) == {
        (homes.projects / "ok").resolve(),
        (homes.projects / "a" / "b").resolve(),
    }, "project memories within the depth limit must be found"
    assert deep.truncated == [], f"everything was scanned at depth 4: {deep.truncated}"

    edge = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),), depth=3))
    assert edge.truncated == [f"{homes.projects.resolve()}: depth limit reached"], (
        "directories below the depth limit must be reported as unscanned"
    )


def test_workbuddy_project_memory_joins_user_instance(homes):
    _touch(homes.workbuddy / "USER.md")
    project = homes.projects / "svc"
    _project_memory(project)
    before = _tree(homes.projects)
    report = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),)))
    assert _tree(homes.projects) == before, "scan wrote files into the project directory"
    results = _host(report, "workbuddy")
    assert len(results) == 1, "project memory must not create an instance"
    assert results[0].memory_roots == (str(homes.workbuddy.resolve()), str(project.resolve())), (
        "project memory must join the user-level instance as a candidate source root"
    )


def test_workbuddy_project_memory_joins_one_instance(homes):
    _touch(homes.workbuddy / "USER.md")
    _touch(homes.codebuddy / ".mcp.json")
    project = homes.projects / "svc"
    _project_memory(project)
    grant = ScanGrant(allowed=True, extra_dirs=(str(homes.projects),))
    community, enterprise = _host(probe_all(grant), "workbuddy")
    assert [result.instance["edition"] for result in (community, enterprise)] == [
        "community",
        "enterprise",
    ], "two user-level editions stay two instances"
    assert community.memory_roots == (str(homes.workbuddy.resolve()), str(project.resolve())), (
        "project memory joins the preferred edition, community"
    )
    assert enterprise.memory_roots == (str(homes.codebuddy.resolve()),), (
        "the other edition must not import the same project logs a second time"
    )
    shutil.rmtree(homes.workbuddy)
    (enterprise,) = _host(probe_all(grant), "workbuddy")
    assert enterprise.instance["edition"] == "enterprise"
    assert enterprise.memory_roots == (str(homes.codebuddy.resolve()), str(project.resolve())), (
        "without a community edition the project memory joins the enterprise edition"
    )


def test_project_without_memory_directory_is_not_a_root(homes):
    _touch(homes.workbuddy / "USER.md")
    _touch(homes.projects / "bare" / ".workbuddy" / "settings.json")
    _project_memory(homes.projects / "svc")
    report = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),)))
    (community,) = _host(report, "workbuddy")
    assert _project_roots(community, homes.projects) == {(homes.projects / "svc").resolve()}, (
        "only projects holding .workbuddy/memory are candidate source roots"
    )


def test_project_memory_without_user_instance_is_only_listed(homes):
    project = homes.projects / "svc"
    _project_memory(project)
    before = _tree(homes.projects)
    report = probe_all(ScanGrant(allowed=True, extra_dirs=(str(homes.projects),)))
    assert _tree(homes.projects) == before, "scan wrote files into the project directory"
    (result,) = _host(report, "workbuddy")
    assert result.state == "suspected", f"expected suspected, got {result.state}"
    assert result.reason == "project memory found but no user-level WorkBuddy"
    assert result.memory_roots == (str(project.resolve()),)


def test_importable_types_follow_capabilities(homes):
    _installed(homes)
    report = probe_all(_ALLOWED)
    importable: dict[str, set[str]] = {}
    for result in report.results:
        importable.setdefault(result.host, set(result.importable))
    assert importable["dsh"] == {"session_turn"}, "DSH has no built-in long-term memory"
    assert "session_turn" not in importable["hermes"], "Hermes session import is unverified"
    assert importable["hermes"] == {"fact", "profile"}
    assert importable["codex"] == {"fact", "session_turn"}
    assert importable["workbuddy"] == {"profile", "fact", "daily_log"}


def test_default_roots_returns_existing_locations(homes):
    assert default_roots() == [], "missing host homes must not be reported as scan roots"
    homes.codex.mkdir()
    homes.codebuddy.mkdir()
    homes.workbuddy.write_text("not a directory", encoding="utf-8")
    assert default_roots() == [homes.codex.resolve(), homes.codebuddy.resolve()], (
        "existing default directories are returned once, in host order"
    )


def test_default_roots_skips_location_it_cannot_stat(homes, monkeypatch):
    homes.codex.mkdir()
    homes.codebuddy.mkdir()
    real_is_dir = Path.is_dir

    def is_dir(self):
        if self == homes.codebuddy:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert default_roots() == [homes.codex.resolve()], (
        "a default location that cannot be stat'ed is left out, not fatal"
    )


def test_missing_host_homes_are_not_reported(homes):
    _touch(homes.hermes / "config.yaml")
    report = probe_all(_ALLOWED)
    assert {result.host for result in report.results} == {"hermes"}, (
        "hosts that are not installed must not appear in the report"
    )
    assert report.skipped == [], f"absent host homes are not access failures: {report.skipped}"


def test_probe_without_grant_returns_nothing(homes):
    _installed(homes)
    before = _tree(homes.root)
    assert probe_all(ScanGrant()) == ScanReport([], [], []), "scanning requires the scan grant"
    assert _tree(homes.root) == before


def test_instance_key_follows_fixed_field_order():
    key = instance_key(
        "hermes", {"profile": "work", "config_root": "/homes/hermes", "ignored": "x"}
    )
    assert key == "hermes|/homes/hermes|work|||", f"unexpected instance key: {key}"


def test_path_view_refuses_paths_outside_roots(tmp_path):
    root = tmp_path / "root"
    (root / "inside").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    view = PathView([root], depth=2, max_entries=100)
    assert view.is_dir(root / "inside")
    assert not view.is_dir(outside), "a path outside the roots must not be visible"
    assert not view.is_file(outside / "x")
    assert view.list_dir(outside) == []
    assert view.walk_for(outside, ".workbuddy") == []
    assert view.skipped == [
        f"{outside}: outside scan roots",
        f"{outside / 'x'}: outside scan roots",
    ], "each refused path is reported once"


def test_walk_finds_marker_in_root_itself(tmp_path):
    root = tmp_path / "project"
    (root / ".workbuddy" / "memory").mkdir(parents=True)
    view = PathView([root], depth=0, max_entries=100)
    assert view.walk_for(root, ".workbuddy") == [root.resolve()]
    assert view.truncated == [f"{root.resolve()}: depth limit reached"], (
        "subdirectories below the depth limit must be reported as unscanned"
    )


def test_extra_roots_are_part_of_the_roots(tmp_path):
    extra = tmp_path / "projects"
    (extra / "svc").mkdir(parents=True)
    view = PathView([], depth=2, max_entries=100, extra_roots=[extra])
    assert view.extra_roots == (extra.resolve(),), "extra roots are kept resolved"
    assert view.is_dir(extra / "svc"), "extra roots are places the view may look at"


def test_walk_reports_entry_limit(tmp_path):
    root = tmp_path / "projects"
    for index in range(6):
        (root / f"p{index}" / ".workbuddy" / "memory").mkdir(parents=True)
    view = PathView([root], depth=4, max_entries=3)
    assert view.walk_for(root, ".workbuddy") == [], "walking must stop at the entry limit"
    assert view.truncated == [f"{root.resolve()}: entry limit reached"], view.truncated


def test_unreadable_directory_is_skipped_not_raised(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "locked" / ".workbuddy" / "memory").mkdir(parents=True)
    (root / "open" / ".workbuddy" / "memory").mkdir(parents=True)
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "locked":
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    view = PathView([root], depth=2, max_entries=100)
    assert view.walk_for(root, ".workbuddy") == [(root / "open").resolve()], (
        "a denied directory must not abort the walk"
    )
    assert any(
        "locked" in note and "Permission denied" in note for note in view.skipped
    ), f"denied directory was not reported: {view.skipped}"


def test_scan_grant_roundtrip_and_version(tmp_path):
    home = MemoryHome(tmp_path / "memory")
    assert load_scan_grant(home) == ScanGrant(), "a missing grant file yields the default grant"
    assert not home.root.exists(), "reading a missing grant must not create the data directory"
    projects = tmp_path / "projects"
    projects.mkdir()
    os.symlink(projects, tmp_path / "alias")
    grant = change_scan_grant(
        ScanGrant(), allowed=True, extra_dirs=(str(tmp_path / "alias"), "", str(projects))
    )
    assert grant.version == 2, "change_scan_grant must bump the version"
    assert grant.allowed is True
    assert grant.extra_dirs == (str(projects.resolve()),), (
        "extra_dirs are stored as resolved real paths, without blanks or duplicates"
    )
    save_scan_grant(home, grant)
    path = home.root / "scan.json"
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"scan grant is not private: {oct(mode)}"
    loaded = load_scan_grant(home)
    assert loaded == grant, "saved grant does not round-trip"
    runtime = replace(loaded, failures={"codex|/c|||": 2}, pending_sync=("abc",), manual=("k",))
    assert runtime.version == loaded.version, "runtime state must not bump the grant version"
    save_scan_grant(home, runtime)
    assert load_scan_grant(home) == runtime, "runtime state does not round-trip"


def test_scan_grant_lock_allows_read_modify_write(tmp_path):
    home = MemoryHome(tmp_path / "memory")
    home.root.mkdir()
    with scan_grant_lock(home):
        grant = load_scan_grant(home)
        save_scan_grant(home, change_scan_grant(grant, allowed=True))
    assert load_scan_grant(home).allowed is True, "nested load/save must not deadlock"


def test_corrupt_grant_is_not_allowed(tmp_path):
    home = MemoryHome(tmp_path / "memory")
    home.root.mkdir()
    path = home.root / "scan.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_scan_grant(home) == ScanGrant(), "a corrupt grant must not grant anything"
    path.write_text('{"allowed": "yes", "auto_connect": 1, "depth": -1}', encoding="utf-8")
    grant = load_scan_grant(home)
    assert grant.allowed is False, "only an explicit true authorizes scanning"
    assert grant.auto_connect is True and grant.depth == 4, "ill-typed fields fall back to defaults"


def test_unreadable_grant_is_not_allowed(tmp_path, monkeypatch):
    home = MemoryHome(tmp_path / "memory")
    home.root.mkdir()
    (home.root / "scan.json").write_text('{"allowed": true}', encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    assert load_scan_grant(home) == ScanGrant(), "a grant that cannot be read grants nothing"


def test_scan_grant_waits_for_another_process(tmp_path):
    home = MemoryHome(tmp_path / "memory")
    home.root.mkdir()
    helper = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(home.root / "scan.json.lock")],
        cwd=Path(__file__).resolve().parents[4],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout.readline().strip() == "locked", "helper did not take the lock"
        started = time.monotonic()
        save_scan_grant(home, ScanGrant(allowed=True))
        waited = time.monotonic() - started
    finally:
        helper.kill()
        helper.wait(timeout=30)
    assert waited >= 0.5, f"grant write did not wait for the lock holder: {waited:.2f}s"
    assert load_scan_grant(home).allowed is True
