"""Scan-driven auto-connect: candidate filtering, batch connect and bookkeeping (F16 D16)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import builtins
import os
from dataclasses import replace
from pathlib import Path

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local import install as install_module
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.install import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_INSTALLING,
    Installation,
    InstanceRef,
)
from jiuwen_memory_entry.local.scan import (
    MANUAL_AFTER_FAILURES,
    ProbeResult,
    ScanGrant,
    ScanReport,
    candidates,
    change_scan_grant,
    connect_results,
    default_locations,
    dismiss_instance,
    include_instance,
    instance_key,
    load_scan_grant,
    resolve_pending_sync,
    save_scan_grant,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def home(tmp_path) -> MemoryHome:
    """A data directory whose user already allowed the scan."""
    memory = MemoryHome(tmp_path / "memory")
    save_scan_grant(memory, ScanGrant(allowed=True))
    return memory


@pytest.fixture
def connect(monkeypatch) -> _Connect:
    """Stand-in for ``install.connect``: records every call, never starts a service."""
    fake = _Connect()
    monkeypatch.setattr(install_module, "connect", fake)
    return fake


class _Connect:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.script: list[object] = []

    def __call__(self, _home, host: str, **kwargs):
        self.calls.append((host, kwargs))
        answer = self.script.pop(0) if self.script else "complete"
        if isinstance(answer, Exception):
            raise answer
        return install_module.ConnectResult(
            install_id=f"{host}-{len(self.calls)}", state=answer, message=answer
        )


def _found(
    host: str,
    instance: dict,
    *,
    memory_roots: tuple[str, ...] | None = None,
    importable: tuple[str, ...] | None = None,
) -> ProbeResult:
    root = instance["config_root"]
    return ProbeResult(
        host=host,
        instance=instance,
        evidence=(f"{root}/config.toml",),
        memory_roots=(root,) if memory_roots is None else memory_roots,
        importable=("fact",) if importable is None else importable,
        state="found",
    )


def _report(*results: ProbeResult) -> ScanReport:
    return ScanReport(list(results), [], [])


def _record(home: MemoryHome, host: str, instance: dict, state: str) -> str:
    """Write an install record the way ``connect`` leaves it."""
    install_id = f"{host}-record"
    install_module._write_record(
        home,
        Installation(
            install_id=install_id,
            host=host,
            connector_id=install_id,
            actor=Scope(org="local", agent=host),
            project="default",
            state=state,
            profile=instance.get("profile", ""),
            edition=instance.get("edition", ""),
            instance=InstanceRef(host, **instance) if instance else None,
        ),
    )
    return install_id


def test_default_locations_are_listed_without_looking(tmp_path, monkeypatch):
    for variable, name in (
        ("JIUWEN_CODEX_HOME", "codex"),
        ("JIUWEN_DSH_HOME", "dsh"),
        ("JIUWEN_HERMES_HOME", "hermes"),
        ("JIUWEN_WORKBUDDY_HOME", "workbuddy"),
        ("JIUWEN_CODEBUDDY_HOME", "codebuddy"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / name))

    def refuse(*_args, **_kwargs):
        raise AssertionError("the consent prompt looked at the filesystem")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "stat", refuse)
    monkeypatch.setattr(Path, "is_dir", refuse)
    monkeypatch.setattr(os, "stat", refuse)
    monkeypatch.setattr(os, "lstat", refuse)

    expected = [tmp_path / name for name in ("codex", "dsh", "hermes", "workbuddy", "codebuddy")]
    assert default_locations() == expected, (
        "every default location, in host order, existing or not"
    )


def test_connected_instance_is_skipped(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    install_id = _record(home, "codex", codex, STATE_CONNECTED)
    report = _report(_found("codex", codex))

    assert candidates(home, report, ScanGrant(allowed=True)) == [], (
        "an instance that is already connected is not a candidate"
    )
    (outcome,) = connect_results(home, report.results)
    assert (outcome.state, outcome.install_id) == ("skipped", install_id), (
        "connecting an already connected instance reports the existing install"
    )
    assert connect.calls == [], "connect must not run again for a connected instance"


def test_disconnected_instance_is_dismissed_and_not_reconnected(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    key = instance_key("codex", codex)
    _record(home, "codex", codex, STATE_DISCONNECTED)
    grant = change_scan_grant(ScanGrant(), allowed=True)
    save_scan_grant(home, grant)
    report = _report(_found("codex", codex))

    assert candidates(home, report, grant) == [], "a disconnected instance is not a candidate"
    stored = load_scan_grant(home)
    assert stored.dismissed == (key,), "the user's disconnect is remembered as ignored"
    assert stored.version == grant.version + 1, "remembering it is an authorization change"
    assert candidates(home, report, stored) == [], "it stays ignored on later scans"
    (outcome,) = connect_results(home, report.results)
    assert outcome.state == "dismissed"
    assert connect.calls == [], "a disconnected instance must never be connected by a scan"


def test_dismissed_or_manual_instance_is_not_connected(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    key = instance_key("codex", codex)
    save_scan_grant(
        home, replace(load_scan_grant(home), dismissed=(key,), manual=(key,))
    )
    report = _report(_found("codex", codex))

    assert candidates(home, report, load_scan_grant(home)) == []
    assert connect_results(home, report.results)[0].state == "dismissed"
    assert connect.calls == [], "an ignored instance is only listed, never connected"


def test_interrupted_install_is_resumed(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    _record(home, "codex", codex, STATE_INSTALLING)
    report = _report(_found("codex", codex))

    assert candidates(home, report, ScanGrant(allowed=True)) == report.results, (
        "an install an earlier scan left incomplete is still a candidate"
    )
    assert connect_results(home, report.results)[0].state == "complete"
    assert len(connect.calls) == 1, "connect resumes the incomplete install"


def test_other_instance_of_the_same_host_is_still_a_candidate(home, connect, tmp_path):
    hermes = tmp_path / "hermes"
    _record(home, "hermes", {"config_root": str(hermes), "profile": "default"}, STATE_CONNECTED)
    work = _found("hermes", {"config_root": str(hermes), "profile": "work"})
    report = _report(work)

    assert candidates(home, report, ScanGrant(allowed=True)) == [work], (
        "profiles are separate instances: one being connected says nothing about the other"
    )
    assert connect_results(home, report.results)[0].state == "complete"


def test_other_edition_of_the_same_host_is_still_a_candidate(home, connect, tmp_path):
    community = {"config_root": str(tmp_path / "workbuddy"), "edition": "community"}
    _record(home, "workbuddy", community, STATE_DISCONNECTED)
    enterprise = _found(
        "workbuddy", {"config_root": str(tmp_path / "codebuddy"), "edition": "enterprise"}
    )
    grant = ScanGrant(allowed=True)
    report = _report(enterprise)

    assert candidates(home, report, grant) == [enterprise], (
        "disconnecting one edition must not ignore the other"
    )
    assert load_scan_grant(home) == ScanGrant(allowed=True), (
        "nothing new was ignored, nothing was written"
    )


def test_only_found_instances_are_connected(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    found = _found("codex", codex)
    suspected = replace(found, state="suspected")
    unavailable = replace(found, state="unavailable", reason="dsh command not found")
    report = _report(found, suspected, unavailable)
    grant = ScanGrant(allowed=True)

    assert candidates(home, report, grant) == [found], (
        "suspected and unavailable instances are listed, not connected"
    )
    outcomes = connect_results(home, report.results)
    assert [outcome.state for outcome in outcomes] == ["complete", "skipped", "unavailable"]
    assert outcomes[2].message == "dsh command not found"
    assert outcomes[1].install_id == "" and outcomes[2].install_id == ""
    assert len(connect.calls) == 1


def test_one_failure_does_not_block_others(home, connect, tmp_path):
    hermes = str(tmp_path / "hermes")
    results = [
        _found("hermes", {"config_root": hermes, "profile": profile}, memory_roots=())
        for profile in ("work", "life", "home")
    ]
    connect.script = ["complete", RuntimeError("config file is locked"), "complete"]

    outcomes = connect_results(home, results)

    assert [outcome.state for outcome in outcomes] == ["complete", "incomplete", "complete"], (
        "the second instance failing must not touch the first and the third"
    )
    assert "config file is locked" in outcomes[1].message, "the reason is kept for the report"
    assert [outcome.install_id for outcome in outcomes if outcome.state == "complete"] == [
        "hermes-1",
        "hermes-3",
    ]
    assert len(connect.calls) == 3, "every instance gets its own attempt"
    assert [outcome.key for outcome in outcomes] == [
        instance_key(result.host, result.instance) for result in results
    ]


def test_background_failures_move_to_manual_after_three(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    key = instance_key("codex", codex)
    connect.script = ["incomplete", RuntimeError("busy"), "incomplete", "complete"]
    results = [_found("codex", codex)]

    connect_results(home, results, background=True)
    assert load_scan_grant(home).failures == {key: 1}, "the first failure is counted"

    connect_results(home, results, background=True)
    grant = load_scan_grant(home)
    assert (grant.failures, grant.manual) == ({key: 2}, ()), "two failures only count"

    connect_results(home, results, background=True)
    grant = load_scan_grant(home)
    assert grant.manual == (key,), f"{MANUAL_AFTER_FAILURES} failures in a row need manual action"
    assert key not in grant.failures, "the count is reset once manual action is needed"
    assert grant.version == ScanGrant().version, "runtime state never moves the version"

    (outcome,) = connect_results(home, results, background=True)
    assert outcome.state == "dismissed", "an instance needing manual action is not retried"
    assert len(connect.calls) == 3, "the fourth run did not call connect"


def test_interactive_failure_is_not_counted(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    connect.script = ["incomplete"] * (MANUAL_AFTER_FAILURES + 1)
    results = [_found("codex", codex)]

    for _ in range(MANUAL_AFTER_FAILURES + 1):
        connect_results(home, results)

    grant = load_scan_grant(home)
    assert (grant.failures, grant.manual) == ({}, ()), (
        "only unattended runs give up on an instance; a user running the scan decides"
    )


def test_success_clears_the_failure_count(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    key = instance_key("codex", codex)
    save_scan_grant(home, replace(ScanGrant(allowed=True), failures={key: 1}))
    connect.script = ["incomplete", "complete"]

    connect_results(home, [_found("codex", codex)], background=True)
    assert load_scan_grant(home).failures == {key: 2}, "a failure extends the streak"
    connect_results(home, [_found("codex", codex)], background=True)

    assert load_scan_grant(home).failures == {}, "a success ends the streak of failures"


def test_successful_connect_marks_pending_sync(home, connect, tmp_path):
    codex = _found(
        "codex", {"config_root": str(tmp_path / "codex")}, importable=("fact", "session_turn")
    )
    bare = _found(
        "hermes",
        {"config_root": str(tmp_path / "hermes"), "profile": "life"},
        memory_roots=(),
    )
    nothing = _found(
        "dsh", {"config_root": str(tmp_path / "dsh"), "profile": "dsh-tui"}, importable=()
    )
    grant = change_scan_grant(ScanGrant(), allowed=True)
    save_scan_grant(home, grant)

    outcomes = connect_results(home, [codex, bare, nothing])

    assert [outcome.state for outcome in outcomes] == ["complete"] * 3, (
        "an instance without memory to import is connected all the same"
    )
    stored = load_scan_grant(home)
    assert stored.pending_sync == ("codex-1",), (
        "only an instance with memory roots and importable types waits for sync authorization"
    )
    assert stored.version == grant.version, "pending sync is runtime state"


def test_pending_sync_is_not_duplicated(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=("codex-1",)))

    connect_results(home, [_found("codex", codex)])

    assert load_scan_grant(home).pending_sync == ("codex-1",), "an install is listed only once"


def test_resolve_pending_sync(home):
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=("codex-1", "dsh-2")))

    resolve_pending_sync(home, "codex-1")

    assert load_scan_grant(home).pending_sync == ("dsh-2",)
    resolve_pending_sync(home, "dsh-2")
    resolve_pending_sync(home, "unknown")
    assert load_scan_grant(home).pending_sync == (), (
        "the last one goes, an unknown id changes nothing"
    )


def test_include_overrides_an_earlier_disconnect(home, connect, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    key = instance_key("codex", codex)
    _record(home, "codex", codex, STATE_DISCONNECTED)
    grant = load_scan_grant(home)
    report = _report(_found("codex", codex))
    assert candidates(home, report, grant) == []

    include_instance(home, key)

    stored = load_scan_grant(home)
    assert candidates(home, report, stored) == report.results, (
        "after scan include the disconnected install no longer counts as ignoring the instance"
    )
    again = load_scan_grant(home)
    assert candidates(home, report, stored) == report.results, "and the next scan agrees"
    assert again == stored, "including once is enough: nothing is dismissed behind the user's back"
    (outcome,) = connect_results(home, report.results)
    assert outcome.state == "complete", "connect goes on where the earlier install left off"
    assert len(connect.calls) == 1


def test_include_restores_dismissed(home, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    work = {"config_root": str(tmp_path / "hermes"), "profile": "work"}
    codex_key, work_key = instance_key("codex", codex), instance_key("hermes", work)
    dismiss_instance(home, "codex", codex)
    grant = load_scan_grant(home)
    dismiss_instance(home, "codex", codex)
    assert load_scan_grant(home) == grant, "dismissing an ignored instance again changes nothing"

    dismiss_instance(home, "hermes", work)
    include_instance(home, codex_key)

    stored = load_scan_grant(home)
    assert stored.dismissed == (work_key,), "only the named instance is included again"
    assert stored.included == (codex_key,), "the explicit decision is what outlasts later scans"
    assert stored.version == grant.version + 2, "one for the second dismissal, one for include"
    (restored,) = candidates(
        home, _report(_found("codex", codex), _found("hermes", work)), stored
    )
    assert restored.host == "codex", "an included instance is a candidate again"

    dismiss_instance(home, "codex", codex)

    assert load_scan_grant(home).included == (), "a later dismissal cancels the inclusion"


def test_include_clears_manual_action(home):
    key = "dsh|/homes/me/.dsh|desktop|||"
    save_scan_grant(home, replace(ScanGrant(allowed=True), manual=(key,), failures={key: 0}))

    include_instance(home, key)

    grant = load_scan_grant(home)
    assert grant.manual == (), "scan include is the way back from needs-manual-action"


def test_include_of_an_instance_that_is_not_ignored_is_a_noop(home):
    grant = replace(ScanGrant(allowed=True, depth=2), dismissed=("a",), manual=("b",))
    save_scan_grant(home, grant)

    include_instance(home, "zzz")

    assert load_scan_grant(home) == grant, "including an instance that is not ignored is a no-op"


def test_connect_uses_default_hooks(home, connect, tmp_path):
    instance = {
        "config_root": str(tmp_path / "workbuddy"),
        "edition": "community",
        "scope": "user",
        "project_root": "",
    }

    connect_results(home, [_found("workbuddy", instance)])

    ((host, kwargs),) = connect.calls
    assert host == "workbuddy"
    assert kwargs["hooks"] is None, "hooks follow the host default; the scan never turns them on"
    assert kwargs["capture"] is None, "capture is left to connect's own default"
    assert (kwargs["edition"], kwargs["scope"], kwargs["project_root"]) == (
        "community",
        "user",
        "",
    ), "the instance found by the probe is the instance connected"
    assert kwargs["confirm"] is None


def test_connect_passes_profile_and_confirm_through(home, connect, tmp_path):
    profile = {"config_root": str(tmp_path / "hermes"), "profile": "work"}

    def confirm(_text: str) -> bool:
        return True

    connect_results(home, [_found("hermes", profile)], confirm=confirm)

    ((_host, kwargs),) = connect.calls
    assert kwargs["profile"] == "work"
    assert kwargs["confirm"] is confirm


def test_disconnect_landing_during_connect_wins(home, monkeypatch, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}
    undone = []

    def connect_while_user_disconnects(_home, host, **_kwargs):
        dismiss_instance(home, host, codex)
        return install_module.ConnectResult(install_id="codex-1", state="complete", message="ok")

    def disconnect(_home, install_id):
        undone.append(install_id)
        return []

    monkeypatch.setattr(install_module, "connect", connect_while_user_disconnects)
    monkeypatch.setattr(install_module, "disconnect", disconnect)

    (outcome,) = connect_results(home, [_found("codex", codex)], background=True)

    assert undone == ["codex-1"], "a connection made against the user's decision is undone"
    assert outcome.state == "dismissed"
    grant = load_scan_grant(home)
    assert grant.pending_sync == () and grant.failures == {}, (
        "an undone connection neither waits for sync nor counts as a failure"
    )


def test_failed_undo_is_reported_as_incomplete(home, monkeypatch, tmp_path):
    codex = {"config_root": str(tmp_path / "codex")}

    def connect_while_user_disconnects(_home, host, **_kwargs):
        dismiss_instance(home, host, codex)
        return install_module.ConnectResult(install_id="codex-1", state="complete", message="ok")

    def disconnect(_home, _install_id):
        raise RuntimeError("config is locked")

    monkeypatch.setattr(install_module, "connect", connect_while_user_disconnects)
    monkeypatch.setattr(install_module, "disconnect", disconnect)

    (outcome,) = connect_results(home, [_found("codex", codex)])

    assert outcome.state == "incomplete", "the user's decision could not be carried out"
    assert "config is locked" in outcome.message, "the reason stays visible"
    assert outcome.install_id == "codex-1", "the install to look at is still named"


def test_connect_results_requires_the_scan_grant(tmp_path, monkeypatch):
    memory = MemoryHome(tmp_path / "memory")
    fake = _Connect()
    monkeypatch.setattr(install_module, "connect", fake)

    (outcome,) = connect_results(memory, [_found("codex", {"config_root": "/homes/me/.codex"})])

    assert outcome.state == "skipped" and outcome.message == "scan is not allowed"
    assert fake.calls == [], "connecting needs the scan grant, not only a list of instances"
    assert load_scan_grant(memory) == ScanGrant(), "refusing writes nothing"


def test_revoked_grant_connects_nothing(tmp_path, home, monkeypatch):
    save_scan_grant(home, change_scan_grant(load_scan_grant(home), allowed=False))
    fake = _Connect()
    monkeypatch.setattr(install_module, "connect", fake)

    (outcome,) = connect_results(home, [_found("codex", {"config_root": "/homes/me/.codex"})])

    assert outcome.state == "skipped"
    assert fake.calls == []


def test_disconnect_dismisses_the_instance(tmp_path, home):
    codex = {"config_root": str(tmp_path / "codex")}
    init_home(home, document_root=tmp_path / "docs", model=ModelSettings())
    install_id = _record(home, "codex", codex, STATE_CONNECTED)

    assert install_module.disconnect(home, install_id) == []

    assert load_scan_grant(home).dismissed == (instance_key("codex", codex),), (
        "an instance the user disconnected is ignored from now on"
    )


def test_disconnect_of_a_record_without_instance_writes_no_grant(tmp_path):
    data_home = MemoryHome(tmp_path / "memory")
    init_home(data_home, document_root=tmp_path / "docs", model=ModelSettings())
    install_id = _record(data_home, "codex", {}, STATE_CONNECTED)

    install_module.disconnect(data_home, install_id)

    assert not (data_home.root / "scan.json").exists(), (
        "a record from before instances were recorded has nothing to ignore"
    )
