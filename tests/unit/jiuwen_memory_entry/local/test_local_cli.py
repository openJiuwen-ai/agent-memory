"""``jiuwen-memory`` CLI skeleton: init and service lifecycle entry points."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import json
from dataclasses import replace

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local import cli
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local import install as install_module
from jiuwen_memory_entry.local import scan as scan_module
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.install import STATE_CONNECTED, Installation, InstanceRef
from jiuwen_memory_entry.local.scan import (
    ConnectOutcome,
    ProbeResult,
    ScanGrant,
    ScanReport,
    instance_key,
    load_scan_grant,
    save_scan_grant,
)
from jiuwen_memory_entry.local.sync.grant import SyncGrant

pytestmark = pytest.mark.unit

_HOST_HOMES = (
    ("JIUWEN_CODEX_HOME", "codex"),
    ("JIUWEN_DSH_HOME", "dsh"),
    ("JIUWEN_HERMES_HOME", "hermes"),
    ("JIUWEN_WORKBUDDY_HOME", "workbuddy"),
    ("JIUWEN_CODEBUDDY_HOME", "codebuddy"),
)


def test_service_status_uninitialized_points_to_init(tmp_path, capsys):
    home = MemoryHome(tmp_path / "home")
    home.run_dir.mkdir(parents=True)
    code = cli.main(["service", "status", "--home", str(home.root)])
    assert code == 1
    assert "run `jiuwen-memory init`" in capsys.readouterr().out


def test_init_creates_service_configuration(tmp_path):
    home = MemoryHome(tmp_path / "home")
    args = [
        "init",
        "--home",
        str(home.root),
        "--document-root",
        str(tmp_path / "docs"),
        "--org",
        "acme",
        "--space",
        "default",
    ]
    assert cli.main(args) == 0
    assert home.service_config.is_file()
    assert cli.main(args) == 1, "second init must refuse to overwrite"
    assert cli.main([*args, "--force"]) == 0


def test_parser_registers_future_subcommands():
    parser = cli._build_parser()
    args = parser.parse_args(["service", "stop", "--force"])
    assert args.command == "service" and args.action == "stop" and args.force is True


def test_stop_removes_a_stale_discovery_file_without_killing(tmp_path, monkeypatch):
    home = MemoryHome(tmp_path / "home")
    home.run_dir.mkdir(parents=True)
    home.discovery_file.write_text(
        json.dumps(
            {"pid": 999999, "port": 1, "protocol": 1, "instance_nonce": "old",
             "home": str(home.root), "started_at": 0.0}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("stale discovery must not be killed")
    ))
    assert cli._stop(home, force=True) == 0
    assert not home.discovery_file.exists()


def test_egress_text_lists_remote_components_only():
    from jiuwen_memory_entry.local import install as install_module

    text = install_module._egress_text(
        [
            {"component": "embedder.default", "destination": "https://e.example",
             "sends": ["query"]},
            {"component": "llm.default", "destination": "local", "sends": []},
        ]
    )
    assert "embedder.default -> https://e.example (query)" in text
    assert "llm.default" not in text


def test_egress_text_distinguishes_unavailable_from_no_outbound():
    from jiuwen_memory_entry.local import install as install_module

    assert "unavailable" in install_module._egress_text([])
    local_only = install_module._egress_text(
        [{"component": "llm.default", "destination": "local", "sends": []}]
    )
    assert "no component sends" in local_only


def test_egress_lookup_failure_is_silent(tmp_path, monkeypatch):
    from jiuwen_memory_entry.local import install as install_module

    class Broken:
        def __init__(self, discovery, token):
            pass

        @staticmethod
        def egress():
            raise RuntimeError("boom")

    monkeypatch.setattr(install_module, "ServiceClient", Broken)
    home = MemoryHome(tmp_path / "home")
    rows = install_module._egress_rows(home, None, "t", object())
    assert rows == []


def test_connect_confirm_allows_non_interactive_capture_off(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    confirm = cli._connect_confirm(SimpleNamespace(yes=False, capture="off"))
    assert confirm("anything") is True
    task = cli._connect_confirm(SimpleNamespace(yes=False, capture="task"))
    assert task("anything") is False
    forced = cli._connect_confirm(SimpleNamespace(yes=True, capture="task"))
    assert forced("anything") is True


def test_op_show_uses_the_authenticated_service(tmp_path, monkeypatch, capsys):
    home = MemoryHome(tmp_path / "home")
    (home.installs_dir / "other").mkdir(parents=True)
    (home.installs_dir / "other" / "token").write_text("other-token\n", encoding="utf-8")
    (home.installs_dir / "owner").mkdir(parents=True)
    (home.installs_dir / "owner" / "token").write_text("owner-token\n", encoding="utf-8")

    class Fake:
        def __init__(self, discovery, token):
            self.token = token

        def post(self, path, payload, **kwargs):
            if self.token != "owner-token":
                return 404, {"error": "NotFound"}
            return 200, {
                "local_op_id": "op-1", "status": "committed", "event_id": "event-1",
                "kernel_operation_id": "kernel-1", "task_id": "task-1",
                "binding_id": "binding-1", "refs": ["u1"],
            }

    monkeypatch.setattr(cli_module(), "ServiceClient", Fake)
    monkeypatch.setattr(
        discovery_module, "live_discovery", lambda _home: object()
    )
    args = type("A", (), {"action": "show", "local_op_id": "op-1",
                          "home": str(home.root), "install": None})
    assert cli_module()._run_op(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["local_op_id"] == "op-1"
    assert payload["event_id"] == "event-1"
    assert payload["kernel_operation_id"] == "kernel-1"
    assert payload["refs"] == ["u1"]


def test_op_show_accepts_a_status_only_response(tmp_path, monkeypatch, capsys):
    home = MemoryHome(tmp_path / "home")
    (home.installs_dir / "owner").mkdir(parents=True)
    (home.installs_dir / "owner" / "token").write_text("t\n", encoding="utf-8")

    class Fake:
        def __init__(self, discovery, token):
            pass

        @staticmethod
        def post(path, payload, **kwargs):
            return 200, {"local_op_id": "op-1", "status": "committed"}

    monkeypatch.setattr(cli_module(), "ServiceClient", Fake)
    monkeypatch.setattr(
        discovery_module, "live_discovery", lambda _home: object()
    )
    args = type("A", (), {"action": "show", "local_op_id": "op-1",
                          "home": str(home.root), "install": "owner"})
    assert cli_module()._run_op(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"local_op_id": "op-1", "status": "committed"}


def cli_module():
    return cli


def test_space_recovery_confirm_does_not_read_connect_args(monkeypatch):
    monkeypatch.setattr(cli_module().sys.stdin, "isatty", lambda: False)
    args = type("Args", (), {"yes": False})  # no capture attribute
    confirm = cli_module()._recover_confirm(args)
    assert confirm("anything") is False
    forced = cli_module()._recover_confirm(type("Args", (), {"yes": True})())
    assert forced("anything") is True


def _initialized(tmp_path) -> MemoryHome:
    home = MemoryHome(tmp_path / "home")
    init_home(home, document_root=tmp_path / "docs", model=ModelSettings())
    return home


def _terminal(monkeypatch, answers: list[str]) -> list[str]:
    """Pretend to be an interactive terminal; ``input`` answers from ``answers``, in order."""
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    return prompts


def _no_terminal(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)


def _fake_host_homes(monkeypatch, tmp_path) -> None:
    """Keep the default locations listed by the CLI inside the fake user directory."""
    for variable, name in _HOST_HOMES:
        monkeypatch.setenv(variable, str(tmp_path / name))


def _probe(monkeypatch, *results: ProbeResult) -> list:
    """Replace ``probe_all``; returns every grant it was called with."""
    calls = []

    def probe_all(grant):
        calls.append(grant)
        return ScanReport(list(results), [], [])

    monkeypatch.setattr(scan_module, "probe_all", probe_all)
    return calls


def _record_connects(monkeypatch) -> list:
    """Replace ``connect_results``; returns ``(args, kwargs)`` of every call."""
    calls = []

    def connect_results(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(scan_module, "connect_results", connect_results)
    return calls


def _codex(tmp_path) -> ProbeResult:
    root = str(tmp_path / "codex")
    return ProbeResult(
        host="codex",
        instance={"config_root": root},
        evidence=(f"{root}/config.toml",),
        memory_roots=(root,),
        importable=("fact", "session_turn"),
        state="found",
    )


def _connected(key: str, install_id: str = "codex-1") -> ConnectOutcome:
    return ConnectOutcome(key, "codex", install_id, "complete", "connected codex")


def test_scan_non_interactive_without_yes_does_not_scan(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    _no_terminal(monkeypatch)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "--home", str(home.root)]) == 0

    output = capsys.readouterr().out
    assert "scan is not allowed" in output
    assert 'run "jiuwen-memory scan" in an interactive terminal' in output, (
        "the way to allow the scan is printed instead"
    )
    assert calls == [] and connects == [], "nothing may be looked at before the user allows it"
    assert not (home.root / "scan.json").exists(), "nobody confirming is not consent"


def test_scan_declined_consent_looks_at_nothing(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    _fake_host_homes(monkeypatch, tmp_path)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)
    prompts = _terminal(monkeypatch, ["n"])

    assert cli.main(["scan", "run", "--home", str(home.root)]) == 0

    output = capsys.readouterr().out
    assert prompts == ["allow scanning? [Y/n] "], "the grant is asked for once, default yes"
    assert str(tmp_path / "codex") in output and str(tmp_path / "codebuddy") in output, (
        "the locations that would be looked at are listed"
    )
    assert "no file content is read" in output
    assert "instances that appear later" in output
    assert calls == [] and connects == [], "a declined grant scans nothing"
    assert load_scan_grant(home) == ScanGrant()
    assert not (home.root / "scan.json").exists(), "a declined grant is not written down"


def test_scan_interactive_consent_defaults_to_yes(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    _fake_host_homes(monkeypatch, tmp_path)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)
    prompts = _terminal(monkeypatch, [""])

    assert cli.main(["scan", "run", "--home", str(home.root)]) == 0

    grant = load_scan_grant(home)
    assert grant.allowed is True and grant.auto_connect is True, (
        "auto-connect is granted together with the first scan"
    )
    assert grant.version == ScanGrant().version + 1
    assert calls == [grant], "the scan runs on the grant as it was just saved"
    assert connects == [] and prompts == ["allow scanning? [Y/n] "], "nothing found, nothing asked"


def test_scan_consent_lists_added_directories_and_depth(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    _fake_host_homes(monkeypatch, tmp_path)
    _probe(monkeypatch)
    projects = tmp_path / "projects"
    projects.mkdir()
    prompts = _terminal(monkeypatch, ["n"])

    assert cli.main(
        ["scan", "run", "--add-dir", str(projects), "--depth", "3", "--home", str(home.root)]
    ) == 0

    output = capsys.readouterr().out
    assert f"{projects.resolve()} (user-added)" in output
    assert "descended at most 3 levels" in output
    assert prompts == ["allow scanning? [Y/n] "]
    assert load_scan_grant(home) == ScanGrant(), "a declined grant saves no option either"


def test_scan_yes_grants_and_applies_options(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    _no_terminal(monkeypatch)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)
    projects = tmp_path / "projects"
    projects.mkdir()

    argv = [
        "scan", "--yes", "--add-dir", str(projects), "--depth", "2", "--home", str(home.root),
    ]
    assert cli.main(argv) == 0, "bare ``scan`` plus options means ``scan run``"

    grant = load_scan_grant(home)
    assert grant.allowed is True, "--yes stands for the user's consent"
    assert grant.extra_dirs == (str(projects.resolve()),), (
        "added directories are stored as real paths"
    )
    assert grant.depth == 2
    assert grant.version == ScanGrant().version + 1, "consent and options are one change"
    assert calls == [grant]
    assert connects == [], "nothing was found, so nothing was connected"

    assert cli.main(["scan", "run", "--remove-dir", str(projects), "--home", str(home.root)]) == 0

    grant = load_scan_grant(home)
    assert grant.extra_dirs == () and grant.depth == 2, "removing one directory keeps the rest"
    assert grant.version == ScanGrant().version + 2


def test_scan_rejects_a_depth_below_one(tmp_path):
    home = _initialized(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["scan", "run", "--depth", "0", "--home", str(home.root)])
    assert stopped.value.code == 2
    assert load_scan_grant(home) == ScanGrant(), "an invalid option changes nothing"


def test_scan_background_without_grant_is_noop(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--background", "--home", str(home.root)]) == 0

    assert calls == [] and connects == []
    assert not (home.root / "scan.json").exists(), "a background run never grants the scan itself"
    assert "scan is not allowed" in capsys.readouterr().out


def test_scan_background_connects_every_candidate_unattended(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    save_scan_grant(home, ScanGrant(allowed=True))
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    prompts = _terminal(monkeypatch, [])
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--background", "--home", str(home.root)]) == 0

    assert connects == [
        ((home, [result]), {"confirm": cli._auto_confirm, "background": True})
    ]
    assert prompts == [], "a background run asks nothing, not even about sync"


def test_scan_background_with_auto_connect_off_only_lists(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    save_scan_grant(home, ScanGrant(allowed=True, auto_connect=False))
    _probe(monkeypatch, _codex(tmp_path))
    _terminal(monkeypatch, [])
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--background", "--home", str(home.root)]) == 0

    assert connects == [], "with auto-connect off new instances are listed, not connected"
    output = capsys.readouterr().out
    assert "found: codex" in output and "auto-connect is off" in output


def test_scan_yes_connects_candidates_without_a_terminal(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    save_scan_grant(home, ScanGrant(allowed=True))
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    _no_terminal(monkeypatch)
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert connects == [((home, [result]), {"confirm": cli._auto_confirm, "background": False})], (
        "without a terminal every candidate takes the default answer"
    )


def test_scan_without_terminal_and_auto_connect_off_dismisses_nothing(
    tmp_path, monkeypatch, capsys
):
    home = _initialized(tmp_path)
    save_scan_grant(home, ScanGrant(allowed=True, auto_connect=False))
    _probe(monkeypatch, _codex(tmp_path))
    _no_terminal(monkeypatch)
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert connects == [], "with auto-connect off nothing is connected unattended"
    assert load_scan_grant(home).dismissed == (), (
        "not being asked is not a dismissal: nothing is remembered against the instance"
    )
    assert "auto-connect is off: nothing was connected" in capsys.readouterr().out


def test_scan_with_auto_connect_off_connects_only_what_is_confirmed(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    save_scan_grant(home, ScanGrant(allowed=True, auto_connect=False))
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    connects = _record_connects(monkeypatch)
    prompts = _terminal(monkeypatch, ["y"])

    assert cli.main(["scan", "run", "--home", str(home.root)]) == 0

    assert prompts == [f"connect codex config_root={tmp_path / 'codex'}? [y/N] "], (
        "with auto-connect off, no is the default but a yes still connects"
    )
    assert connects == [((home, [result]), {"confirm": cli._auto_confirm, "background": False})]


def test_scan_declined_instance_is_dismissed(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    prompts = _terminal(monkeypatch, ["n"])
    connects = _record_connects(monkeypatch)

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert prompts == [f"connect codex config_root={tmp_path / 'codex'}? [Y/n] "], (
        "each found instance is offered once, default yes"
    )
    assert load_scan_grant(home).dismissed == (instance_key("codex", result.instance),)
    assert connects == [], "a declined instance is not connected"
    assert "scan include" in capsys.readouterr().out, "the way back is printed"


def test_scan_dry_run_lists_without_connecting(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    root = str(tmp_path / "hermes")
    suspected = ProbeResult(
        host="hermes",
        instance={"config_root": root, "profile": "default"},
        evidence=(f"{root}/config.yaml",),
        memory_roots=(),
        importable=("fact", "profile"),
        state="suspected",
    )
    _probe(monkeypatch, _codex(tmp_path), suspected)
    _no_terminal(monkeypatch)
    connects = _record_connects(monkeypatch)

    argv = ["scan", "run", "--yes", "--dry-run", "--home", str(home.root)]
    assert cli.main(argv) == 0

    output = capsys.readouterr().out
    assert "scan: 1 instance(s) to connect, 1 only listed" in output
    assert "found: codex config_root=" in output
    assert f"  key: {instance_key('codex', {'config_root': str(tmp_path / 'codex')})}" in output
    assert f"  evidence: {tmp_path / 'codex' / 'config.toml'}" in output
    assert "  hooks: on by default" in output and "  importable: fact, session_turn" in output
    assert "suspected: hermes profile=default config_root=" in output
    assert "  hooks: off by default" in output
    assert connects == [], "a dry run lists only"


def test_scan_offers_sync_authorization_per_connected_install(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    key = instance_key("codex", result.instance)
    monkeypatch.setattr(scan_module, "connect_results", lambda *_a, **_k: [_connected(key)])
    syncs = []

    def run_sync(namespace):
        syncs.append(namespace)
        return 0

    monkeypatch.setattr(cli, "_run_sync", run_sync)
    prompts = _terminal(monkeypatch, ["", "y"])

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert prompts == [
        f"connect codex config_root={tmp_path / 'codex'}? [Y/n] ",
        "authorize syncing codex's existing memory now? [y/N] ",
    ], "connecting defaults to yes, authorizing sync defaults to no"
    (namespace,) = syncs
    assert namespace.sync_action == "grant"
    assert namespace.host == "codex" and namespace.install == "codex-1"
    assert namespace.home == str(home.root)
    assert namespace.dir == list(result.memory_roots)
    assert namespace.types == "fact", "sessions are not offered by default"
    assert namespace.sessions is False and namespace.yes is False


def test_scan_declined_sync_authorization_leaves_the_install_waiting(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    key = instance_key("codex", result.instance)
    monkeypatch.setattr(
        scan_module, "connect_results", lambda *_a, **_k: [_connected(key)]
    )
    syncs = []
    monkeypatch.setattr(cli, "_run_sync", lambda namespace: syncs.append(namespace) or 0)
    prompts = _terminal(monkeypatch, ["", "n"])

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert len(prompts) == 2 and syncs == [], "a no to sync is final for this run"


def test_scan_does_not_offer_sync_for_an_instance_without_memory(tmp_path, monkeypatch):
    home = _initialized(tmp_path)
    root = str(tmp_path / "hermes")
    result = ProbeResult(
        host="hermes",
        instance={"config_root": root, "profile": "life"},
        evidence=(f"{root}/config.yaml",),
        memory_roots=(),
        importable=("fact", "profile"),
        state="found",
    )
    _probe(monkeypatch, result)
    key = instance_key("hermes", result.instance)
    monkeypatch.setattr(
        scan_module, "connect_results",
        lambda *_a, **_k: [ConnectOutcome(key, "hermes", "hermes-1", "complete")],
    )
    syncs = []
    monkeypatch.setattr(cli, "_run_sync", lambda namespace: syncs.append(namespace) or 0)
    prompts = _terminal(monkeypatch, [""])

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    assert len(prompts) == 1 and syncs == [], "nothing to import means nothing to authorize"
    assert prompts[0].startswith("connect hermes profile=life "), "the only question is the connect"


def test_scan_reports_a_failed_connect(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    result = _codex(tmp_path)
    _probe(monkeypatch, result)
    key = instance_key("codex", result.instance)
    _no_terminal(monkeypatch)
    failed = ConnectOutcome(key, "dsh", "", "incomplete", "config is locked")
    monkeypatch.setattr(
        scan_module, "connect_results", lambda *_a, **_k: [_connected(key), failed]
    )

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 1

    output = capsys.readouterr().out
    assert "complete: codex install=codex-1 (connected codex)" in output
    assert "incomplete: dsh (config is locked)" in output


def test_scan_reports_what_was_not_scanned(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    report = ScanReport([], ["/projects: depth limit reached"], ["/projects/locked: denied"])
    monkeypatch.setattr(scan_module, "probe_all", lambda grant: report)
    _no_terminal(monkeypatch)

    assert cli.main(["scan", "run", "--yes", "--home", str(home.root)]) == 0

    output = capsys.readouterr().out
    assert "scan: 0 instance(s) to connect, 0 only listed" in output
    assert "not fully scanned: /projects: depth limit reached" in output
    assert "skipped: /projects/locked: denied" in output


def test_scan_commands_need_an_initialized_home(tmp_path, capsys):
    home = MemoryHome(tmp_path / "home")
    commands = (
        ["scan"],
        ["scan", "status"],
        ["scan", "include", "k"],
        ["scan", "auto-connect", "off"],
        ["scan", "revoke"],
    )
    for argv in commands:
        assert cli.main([*argv, "--home", str(home.root)]) == 1, argv
        assert "run `jiuwen-memory init`" in capsys.readouterr().out
    assert not home.root.exists(), "nothing is written into an uninitialized data directory"


def test_scan_status_lists_the_grant(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    _fake_host_homes(monkeypatch, tmp_path)
    key = instance_key("codex", {"config_root": "/homes/me/.codex"})
    grant = replace(
        ScanGrant(allowed=True, extra_dirs=(str(tmp_path / "projects"),)),
        dismissed=(key,),
        manual=("dsh|/homes/me/.dsh|desktop|||",),
        pending_sync=("codex-1",),
    )
    save_scan_grant(home, grant)

    assert cli.main(["scan", "status", "--home", str(home.root)]) == 0

    output = capsys.readouterr().out
    assert "scan: allowed (v1)" in output
    assert f"  {tmp_path / 'codebuddy'}" in output, "default locations are listed"
    assert f"{tmp_path / 'projects'} (user-added)" in output
    assert "depth: 4" in output and "auto-connect: on" in output
    assert f"dismissed: {key}" in output
    assert "needs manual action: dsh|/homes/me/.dsh|desktop|||" in output
    assert "waiting for sync authorization:" in output
    assert "  codex-1 (no install record)" in output


def test_scan_auto_connect_toggles_and_bumps_the_version(tmp_path, capsys):
    home = _initialized(tmp_path)

    assert cli.main(["scan", "auto-connect", "off", "--home", str(home.root)]) == 0

    grant = load_scan_grant(home)
    assert grant.auto_connect is False and grant.version == ScanGrant().version + 1
    assert "auto-connect: off" in capsys.readouterr().out
    assert cli.main(["scan", "auto-connect", "on", "--home", str(home.root)]) == 0
    assert load_scan_grant(home).auto_connect is True


def test_scan_include_restores_only_a_known_instance(tmp_path, capsys):
    home = _initialized(tmp_path)
    key = instance_key("codex", {"config_root": "/homes/me/.codex"})
    grant = replace(ScanGrant(allowed=True), dismissed=(key, "hermes|/homes/me/.hermes|work|||"))
    save_scan_grant(home, grant)

    assert cli.main(["scan", "include", "nope", "--home", str(home.root)]) == 1

    assert "not ignored: nope" in capsys.readouterr().err
    assert cli.main(["scan", "include", key, "--home", str(home.root)]) == 0
    stored = load_scan_grant(home)
    assert stored.dismissed == ("hermes|/homes/me/.hermes|work|||",), "only the named one is back"
    assert stored.version == grant.version + 1


def test_scan_revoke_stops_the_scan_and_keeps_the_rest(tmp_path):
    home = _initialized(tmp_path)
    key = instance_key("codex", {"config_root": "/homes/me/.codex"})
    grant = replace(
        ScanGrant(allowed=True, extra_dirs=(str(tmp_path / "projects"),)),
        dismissed=(key,),
        pending_sync=("codex-1",),
    )
    save_scan_grant(home, grant)
    install_id = "codex-1"
    install_module._write_record(
        home,
        Installation(
            install_id=install_id,
            host="codex",
            connector_id=install_id,
            actor=Scope(org="local", agent="a"),
            project="default",
            state=STATE_CONNECTED,
            sync={"grant_id": "g", "version": 1, "roots": ["/mem"], "types": ["fact"]},
            instance=InstanceRef("codex", config_root="/homes/me/.codex"),
        ),
    )
    record = home.installs_dir / install_id / "install.json"
    before = record.read_bytes()

    assert cli.main(["scan", "revoke", "--home", str(home.root)]) == 0

    stored = load_scan_grant(home)
    assert stored.allowed is False and stored.version == grant.version + 1
    assert stored.extra_dirs == grant.extra_dirs and stored.dismissed == (key,)
    assert stored.pending_sync == ("codex-1",), "revoking the scan decides no sync authorization"
    assert record.read_bytes() == before, "an existing install, sync grant included, is untouched"


def test_init_non_interactive_prints_scan_hint(tmp_path, monkeypatch, capsys):
    home = MemoryHome(tmp_path / "home")
    _no_terminal(monkeypatch)
    calls = _probe(monkeypatch)

    argv = ["init", "--home", str(home.root), "--document-root", str(tmp_path / "docs")]
    assert cli.main(argv) == 0

    output = capsys.readouterr().out
    assert 'run "jiuwen-memory scan" to find and connect local agents' in output
    assert calls == [], "init never scans on its own"
    assert not (home.root / "scan.json").exists()


def test_init_interactive_offers_the_scan(tmp_path, monkeypatch):
    home = MemoryHome(tmp_path / "home")
    _fake_host_homes(monkeypatch, tmp_path)
    calls = _probe(monkeypatch)
    connects = _record_connects(monkeypatch)
    prompts = _terminal(monkeypatch, [""])

    argv = ["init", "--home", str(home.root), "--document-root", str(tmp_path / "docs")]
    assert cli.main(argv) == 0

    assert home.service_config.is_file()
    assert prompts == ["allow scanning? [Y/n] "], "a successful init offers the scan grant"
    assert load_scan_grant(home).allowed is True and len(calls) == 1
    assert connects == [], "nothing was found under the fake homes"


def test_pending_sync_hint_precedes_other_commands(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    _fake_host_homes(monkeypatch, tmp_path)
    _terminal(monkeypatch, [])
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=("codex-1", "dsh-2")))

    assert cli.main(["service", "status", "--home", str(home.root)]) == 1

    err = capsys.readouterr().err
    assert (
        '2 agent(s) connected by scan are waiting for sync authorization: '
        'run "jiuwen-memory scan status"'
    ) in err

    assert cli.main(["scan", "status", "--home", str(home.root)]) == 0

    captured = capsys.readouterr()
    assert captured.err == "", "scan itself does not repeat the reminder"
    assert "  codex-1 (no install record)" in captured.out
    assert "  dsh-2 (no install record)" in captured.out


def test_pending_sync_hint_needs_a_terminal_and_something_pending(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=("codex-1",)))
    _no_terminal(monkeypatch)

    assert cli.main(["service", "status", "--home", str(home.root)]) == 1

    assert capsys.readouterr().err == "", "the reminder is for a person at the terminal"
    _terminal(monkeypatch, [])
    save_scan_grant(home, ScanGrant(allowed=True))
    assert cli.main(["service", "status", "--home", str(home.root)]) == 1
    assert capsys.readouterr().err == "", "nothing pending means nothing to remind"


def test_sync_grant_stops_the_pending_sync_reminder(tmp_path, monkeypatch, capsys):
    home = _initialized(tmp_path)
    install_id = "codex-1"
    install_module._write_record(
        home,
        Installation(
            install_id=install_id,
            host="codex",
            connector_id=install_id,
            actor=Scope(org="local", agent="a"),
            project="default",
            state=STATE_CONNECTED,
            instance=InstanceRef("codex", config_root=str(tmp_path / "codex")),
        ),
    )
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=(install_id,)))
    source = tmp_path / "codex" / "memories"
    source.mkdir(parents=True)

    argv = [
        "sync", "grant", "codex", "--install", install_id, "--dir", str(source), "--yes",
        "--home", str(home.root),
    ]
    assert cli.main(argv) == 0, capsys.readouterr().out

    assert load_scan_grant(home).pending_sync == (), "a granted install no longer waits"
    stored = install_module._load_install(home, install_id).sync
    assert SyncGrant.from_dict(stored).roots == (str(source.resolve()),)
