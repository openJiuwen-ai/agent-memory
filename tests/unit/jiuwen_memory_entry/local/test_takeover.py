"""Takeover of a host's own memory: offering conditions, record and restore (F16 D16 §7)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from jiuwen_memory.api import Scope
from jiuwen_memory_entry.local import cli
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local import install as install_module
from jiuwen_memory_entry.local import scan as scan_module
from jiuwen_memory_entry.local.home import MemoryHome, ModelSettings, init_home
from jiuwen_memory_entry.local.hosts import (
    CAPABILITIES,
    EntryRef,
    HostCapabilities,
    HostInstall,
    registry,
)
from jiuwen_memory_entry.local.hosts.registry import HOSTS, HostSpec
from jiuwen_memory_entry.local.install import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    Installation,
    InstanceRef,
    set_hooks,
    set_takeover,
    takeover_blocker,
)
from jiuwen_memory_entry.local.scan import (
    ProbeResult,
    ScanGrant,
    instance_key,
    load_scan_grant,
    save_scan_grant,
)
from jiuwen_memory_entry.local.sync.grant import change_grant, new_grant

pytestmark = pytest.mark.unit

_HOST = "fake"
_INSTALL = "fake-1"
_SUPPORTED = ("supported", "supported", "supported", "supported", "supported")


class _OwnMemory:
    """TakeoverWriter double: the host's own-memory switch is ``memory`` in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []
        self.restore_error: Exception | None = None

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    def current(self, install: HostInstall) -> object | None:
        return self._read().get("memory")

    def disable(self, install: HostInstall) -> EntryRef:
        self.calls.append("disable")
        data = self._read()
        entry = EntryRef(path=self.path, section="memory", value=data.get("memory"))
        self._write({**data, "memory": False})
        return entry

    def restore(self, install: HostInstall, entry: EntryRef) -> bool:
        self.calls.append("restore")
        if self.restore_error is not None:
            raise self.restore_error
        data = self._read()
        if data.get("memory") is not False:
            return False
        if entry.value is None:
            data.pop("memory", None)
        else:
            data["memory"] = entry.value
        self._write(data)
        return True


class _Layer:
    """MCP and hook writer double: nothing is written, nothing is kept."""

    @staticmethod
    def merge(install: HostInstall) -> list[EntryRef]:
        return []

    @staticmethod
    def verify(install: HostInstall) -> bool:
        return True

    @staticmethod
    def rollback(install: HostInstall, entries: list[EntryRef]) -> list[EntryRef]:
        return []


class _Service:
    """Service double answering ``/local/sync/status`` with one fixed body."""

    status = 200
    body: dict = {"unconfirmed": []}

    def __init__(self, _discovery, _token) -> None:
        pass

    @staticmethod
    def post(path: str, payload: dict, **_kwargs):
        assert path == "/local/sync/status", f"unexpected request: {path}"
        return _Service.status, _Service.body


class _ConnectService:
    """Service double good enough for a reconnect: no egress, no reload delay."""

    def __init__(self, _discovery, _token) -> None:
        pass

    @staticmethod
    def egress() -> list[dict]:
        return []

    @staticmethod
    def reload() -> None:
        pass

    @staticmethod
    def status() -> dict:
        return {"instance_nonce": "nonce", "reloading": False}


@pytest.fixture
def own_memory(tmp_path, monkeypatch) -> _OwnMemory:
    """Register ``fake``, a host whose own-memory switch is a JSON file under ``tmp_path``."""
    path = tmp_path / "fake" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"memory": True, "theme": "dark"}), encoding="utf-8")
    writer = _OwnMemory(path)
    capabilities = HostCapabilities(*_SUPPORTED, takeover="supported")
    monkeypatch.setitem(CAPABILITIES, _HOST, capabilities)
    monkeypatch.setitem(
        registry.HOSTS,
        _HOST,
        HostSpec(
            _HOST,
            capabilities,
            mcp_writer=_Layer,
            hook_writer=_Layer,
            instance=lambda **_kwargs: {"config_root": str(tmp_path / "fake")},
            takeover=lambda: writer,
        ),
    )
    monkeypatch.setattr(cli, "HOST_NAMES", (*cli.HOST_NAMES, _HOST))
    monkeypatch.setattr(install_module, "SUPPORTED_HOSTS", set(registry.HOSTS))
    return writer


@pytest.fixture
def home(tmp_path) -> MemoryHome:
    memory = MemoryHome(tmp_path / "memory")
    init_home(memory, document_root=tmp_path / "docs", model=ModelSettings())
    return memory


@pytest.fixture
def service(monkeypatch) -> type[_Service]:
    """A running service; the test sets what ``/local/sync/status`` answers."""
    monkeypatch.setattr(cli, "live_discovery", lambda _home: object())
    monkeypatch.setattr(cli, "ServiceClient", _Service)
    _Service.status = 200
    _Service.body = {"unconfirmed": []}
    return _Service


def _connected(
    home: MemoryHome,
    *,
    host: str = _HOST,
    install_id: str = _INSTALL,
    profile: str = "",
    hooks: bool = True,
    sync: dict | None = None,
) -> str:
    """Write a connected install record, token included, the way ``connect`` leaves it."""
    install_module._write_record(
        home,
        Installation(
            install_id=install_id,
            host=host,
            connector_id=install_id,
            actor=Scope(org="local", agent=host),
            project="default",
            state=STATE_CONNECTED,
            hooks=hooks,
            sync=sync or {},
            profile=profile,
            instance=InstanceRef(host, config_root=f"/homes/me/.{host}"),
        ),
    )
    token = home.installs_dir / install_id / "token"
    token.write_text("token\n", encoding="utf-8")
    return install_id


def _grant(own_memory: _OwnMemory, *, revoked: bool = False) -> dict:
    grant = new_grant(
        [str(own_memory.path.parent)], types={"fact"}, target_space="default"
    )
    return (change_grant(grant, revoked=True) if revoked else grant).as_dict()


def _synced(_install: Installation) -> bool:
    return True


def _record(home: MemoryHome, install_id: str = _INSTALL) -> Installation:
    return install_module._load_install(home, install_id)


def _argv(action: str, home: MemoryHome, *options: str) -> list[str]:
    return ["takeover", action, _HOST, *options, "--home", str(home.root)]


def test_takeover_is_unverified_until_measured_and_no_host_writes_it_yet():
    assert HostCapabilities(*_SUPPORTED).takeover == "unverified", (
        "the capability comes last with a default, so existing declarations stand"
    )
    assert CAPABILITIES["dsh"].takeover == "unsupported", "DSH has no own memory to take over"
    assert {name for name, item in CAPABILITIES.items() if item.takeover != "unverified"} == {
        "dsh"
    }, "every other host waits for a measurement"
    assert {name for name, spec in HOSTS.items() if spec.takeover is not None} == set(), (
        "no real host offers a takeover writer before its setting was measured"
    )
    assert "takeover" in {field.name for field in fields(HostSpec)}


def test_takeover_unavailable_without_hooks(own_memory, home):
    _connected(home, hooks=False)

    result = set_takeover(home, _INSTALL, True, sync_ok=_synced)

    assert result.state == "unsupported", result.message
    assert "hooks" in result.message, "the reason names what is missing"
    assert own_memory.calls == [], "the host configuration is not touched"
    assert own_memory.current(None) is True
    assert _record(home).takeover == {}


def test_takeover_unavailable_when_unverified(home, monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    asked = []

    def sync_ok(install: Installation) -> bool:
        asked.append(install)
        return True

    _connected(home, host="codex", install_id="codex-1")

    result = set_takeover(home, "codex-1", True, sync_ok=sync_ok)

    assert result.state == "unsupported", "the real Codex host has not been measured"
    assert "unverified" in result.message
    assert takeover_blocker(_record(home, "codex-1")) == result.message
    assert asked == [], "an unverified setting is refused before the sync is looked at"
    assert not (tmp_path / "codex").exists(), "nothing was looked at in the Codex home"


@pytest.mark.parametrize(
    ("hooks", "capability", "writer", "reason"),
    [
        (False, "supported", True, "hooks are off"),
        (True, "unverified", True, "is unverified"),
        (True, "supported", False, "no takeover writer"),
    ],
)
def test_takeover_blockers_are_checked_in_order(
    own_memory, home, monkeypatch, hooks, capability, writer, reason
):
    monkeypatch.setitem(CAPABILITIES, _HOST, replace(CAPABILITIES[_HOST], takeover=capability))
    spec = HOSTS[_HOST]
    monkeypatch.setitem(
        registry.HOSTS, _HOST, replace(spec, takeover=spec.takeover if writer else None)
    )
    _connected(home, hooks=hooks)

    result = set_takeover(home, _INSTALL, True, sync_ok=_synced)

    assert result.state == "unsupported" and reason in result.message, result.message
    assert own_memory.calls == [] and _record(home).takeover == {}


def test_takeover_requires_completed_sync_and_leaves_config_untouched(own_memory, home):
    _connected(home, sync=_grant(own_memory))
    prompts = []

    result = set_takeover(
        home,
        _INSTALL,
        True,
        confirm=lambda text: prompts.append(text) or True,
        sync_ok=lambda _install: False,
    )

    assert result.state == "incomplete"
    assert result.message == "sync must complete before takeover"
    assert own_memory.calls == [], "nothing is switched off before the host's memory is imported"
    assert own_memory.current(None) is True
    assert prompts == [], "the user is not asked while the precondition fails"
    assert _record(home).takeover == {}


def test_takeover_disable_and_restore_roundtrip(own_memory, home):
    _connected(home)
    prompts = []

    on = set_takeover(
        home, _INSTALL, True, confirm=lambda text: prompts.append(text) or True,
        sync_ok=_synced,
    )

    assert on.state == "complete", on.message
    assert own_memory._read() == {"memory": False, "theme": "dark"}, (
        "only the own-memory switch changes"
    )
    assert _record(home).takeover == {
        "entry": {
            "path": str(own_memory.path),
            "section": "memory",
            "value": True,
            "raw": "",
            "layer": "mcp",
            "created_file": False,
        }
    }, "the original value travels with the entry"
    assert len(prompts) == 1 and "not deleted" in prompts[0], (
        "one confirmation says what is going to change"
    )
    again = set_takeover(home, _INSTALL, True, sync_ok=_synced)

    assert again.state == "complete" and "already taken over" in again.message
    assert own_memory.calls == ["disable"], (
        "taking over twice must not record the switched-off value as the original"
    )

    off = set_takeover(home, _INSTALL, False, sync_ok=_synced)

    assert off.state == "complete", off.message
    assert own_memory._read() == {"memory": True, "theme": "dark"}, "the original value is back"
    assert _record(home).takeover == {}, "nothing is left to restore"
    assert own_memory.calls == ["disable", "restore"]
    assert set_takeover(home, _INSTALL, False, sync_ok=_synced).state == "complete"
    assert own_memory.calls == ["disable", "restore"], "turning it off twice restores once"


def test_takeover_waits_for_the_users_confirmation(own_memory, home):
    _connected(home)

    result = set_takeover(home, _INSTALL, True, confirm=lambda _text: False, sync_ok=_synced)

    assert result.state == "aborted"
    assert "was not changed" in result.message
    assert own_memory.calls == [] and own_memory.current(None) is True
    assert _record(home).takeover == {}


def test_restore_keeps_user_modified_value(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory._write({**own_memory._read(), "memory": "sometimes"})

    result = set_takeover(home, _INSTALL, False, sync_ok=_synced)

    assert result.state == "complete"
    assert "user" in result.message and "kept" in result.message, (
        "the report says the user's value was kept"
    )
    assert own_memory._read()["memory"] == "sometimes", "the user's value is not overwritten"
    assert _record(home).takeover == {}, "nothing is left to restore"


def test_failed_restore_is_reported_and_stays_retryable(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory.restore_error = OSError("config is locked")

    failed = set_takeover(home, _INSTALL, False, sync_ok=_synced)

    assert failed.state == "incomplete" and "config is locked" in failed.message
    assert _record(home).takeover, "the original value stays recorded for a retry"
    own_memory.restore_error = None
    retried = set_takeover(home, _INSTALL, False, sync_ok=_synced)

    assert retried.state == "complete" and own_memory.current(None) is True


def test_record_from_before_takeover_loads_as_not_taken_over(own_memory, home):
    _connected(home)
    path = home.installs_dir / _INSTALL / "install.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["takeover"]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _record(home).takeover == {}, "an old record is not under takeover"
    result = set_takeover(home, _INSTALL, False, sync_ok=_synced)

    assert result.state == "complete" and "not taken over" in result.message


def test_disconnect_restores_takeover(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)

    kept = install_module.disconnect(home, _INSTALL)

    assert kept == [], "a clean restore keeps nothing"
    assert own_memory._read() == {"memory": True, "theme": "dark"}, (
        "disconnect puts the host's own memory back"
    )
    record = _record(home)
    assert (record.state, record.takeover) == (STATE_DISCONNECTED, {})


def test_disconnect_reports_a_setting_the_user_changed(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory._write({**own_memory._read(), "memory": "sometimes"})

    kept = install_module.disconnect(home, _INSTALL)

    assert [(entry.path, entry.section) for entry in kept] == [(own_memory.path, "memory")], (
        "the setting that stayed is returned among the kept entries"
    )
    assert own_memory._read()["memory"] == "sometimes"
    assert _record(home).takeover == {}


def test_disconnect_is_not_blocked_by_a_failing_restore(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory.restore_error = OSError("config is locked")

    kept = install_module.disconnect(home, _INSTALL)

    assert [entry.section for entry in kept] == ["memory"], "the unrestored setting is named"
    record = _record(home)
    assert record.state == STATE_DISCONNECTED, "the disconnect itself goes through"
    assert record.takeover, "the original value stays with the disconnected record"


def test_disabling_hooks_restores_takeover(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)

    result = set_hooks(home, _INSTALL, False)

    assert result.state == "complete", result.message
    assert "hooks disabled" in result.message and "own memory" in result.message
    assert own_memory.current(None) is True
    record = _record(home)
    assert (record.hooks, record.takeover) == (False, {})


def test_disabling_hooks_goes_on_when_the_restore_fails(own_memory, home):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory.restore_error = OSError("config is locked")

    result = set_hooks(home, _INSTALL, False)

    assert result.state == "complete" and "config is locked" in result.message
    record = _record(home)
    assert (record.hooks, bool(record.takeover)) == (False, True), (
        "the hooks go off all the same; the record stays for a retry"
    )


def test_reconnect_without_hooks_restores_takeover(own_memory, home, monkeypatch):
    _connected(home)
    assert set_takeover(home, _INSTALL, True, sync_ok=_synced).state == "complete"
    assert own_memory.current(None) is False
    discovery = discovery_module.Discovery(
        pid=1, port=1, protocol=1, instance_nonce="nonce",
        home=str(home.root), started_at=0.0,
    )
    monkeypatch.setattr(
        install_module.discovery_module, "ensure_service", lambda *a, **k: discovery
    )
    monkeypatch.setattr(install_module, "ServiceClient", _ConnectService)
    monkeypatch.setattr(install_module, "_ensure_space", lambda *a, **k: None)

    result = install_module.connect(home, _HOST, hooks=False, confirm=lambda _text: True)

    assert result.state == "complete", result.message
    assert "own memory" in result.message, "the restore is reported with the connect"
    assert own_memory.current(None) is True, "the host's own memory is switched back on"
    record = _record(home)
    assert (record.hooks, record.takeover) == (False, {}), "nothing is left to restore"


def test_reconnect_keeps_going_when_the_restore_fails(own_memory, home, monkeypatch):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    own_memory.restore_error = OSError("config is locked")
    discovery = discovery_module.Discovery(
        pid=1, port=1, protocol=1, instance_nonce="nonce",
        home=str(home.root), started_at=0.0,
    )
    monkeypatch.setattr(
        install_module.discovery_module, "ensure_service", lambda *a, **k: discovery
    )
    monkeypatch.setattr(install_module, "ServiceClient", _ConnectService)
    monkeypatch.setattr(install_module, "_ensure_space", lambda *a, **k: None)

    result = install_module.connect(home, _HOST, hooks=False, confirm=lambda _text: True)

    assert result.state == "complete", result.message
    assert "config is locked" in result.message, "the failure is reported, not swallowed"
    record = _record(home)
    assert record.hooks is False and record.takeover, (
        "the reconnect goes through; the original value stays for a retry"
    )


def test_background_connect_never_takes_over(own_memory, home, monkeypatch):
    takeovers = []
    monkeypatch.setattr(
        install_module, "set_takeover", lambda *args, **kwargs: takeovers.append((args, kwargs))
    )
    monkeypatch.setattr(
        install_module,
        "connect",
        lambda _home, _host, **_kwargs: install_module.ConnectResult(_INSTALL, "complete"),
    )
    save_scan_grant(home, ScanGrant(allowed=True))
    instance = {"config_root": "/homes/me/.fake"}
    found = ProbeResult(
        host=_HOST,
        instance=instance,
        evidence=("/homes/me/.fake/settings.json",),
        memory_roots=(instance["config_root"],),
        importable=("fact",),
        state="found",
    )

    (outcome,) = scan_module.connect_results(home, [found], background=True)

    assert outcome.state == "complete"
    assert takeovers == [], "an unattended connect never takes the host's own memory over"
    assert own_memory.calls == [] and own_memory.current(None) is True


def test_takeover_cli_refuses_an_unverified_host_with_exit_code_3(
    home, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    _connected(home, host="codex", install_id="codex-1")

    argv = ["takeover", "on", "codex", "--yes", "--home", str(home.root)]
    assert cli.main(argv) == 3

    output = capsys.readouterr().out
    assert "unsupported: install=codex-1" in output and "unverified" in output
    assert not (tmp_path / "codex").exists(), "nothing was looked at in the Codex home"


def test_takeover_cli_takes_over_once_the_sync_is_done(own_memory, home, service, capsys):
    _connected(home, sync=_grant(own_memory))

    assert cli.main(_argv("on", home, "--yes")) == 0, capsys.readouterr().out

    assert own_memory.current(None) is False
    assert _record(home).takeover["entry"]["value"] is True


def test_takeover_cli_off_restores_the_host(own_memory, home, service, capsys):
    _connected(home, sync=_grant(own_memory))
    assert cli.main(_argv("on", home, "--yes")) == 0
    capsys.readouterr()

    assert cli.main(_argv("off", home)) == 0

    output = capsys.readouterr().out
    assert "complete: install=fake-1 fake's own memory is back on" in output
    assert own_memory.current(None) is True and _record(home).takeover == {}


@pytest.mark.parametrize("situation", ["no grant", "revoked", "service down", "unconfirmed"])
def test_takeover_cli_waits_while_the_sync_is_not_done(
    own_memory, home, service, monkeypatch, capsys, situation
):
    sync = _grant(own_memory, revoked=situation == "revoked") if situation != "no grant" else {}
    if situation == "service down":
        monkeypatch.setattr(cli, "live_discovery", lambda _home: None)
    if situation == "unconfirmed":
        service.body = {"unconfirmed": [{"op": "op-1", "sources": []}]}
    _connected(home, sync=sync)

    assert cli.main(_argv("on", home, "--yes")) == 1, situation

    output = capsys.readouterr().out
    assert "incomplete: install=fake-1 sync must complete before takeover" in output, situation
    assert own_memory.current(None) is True, "the host's own memory stays on"
    assert _record(home).takeover == {}


def test_takeover_status_shows_the_recorded_setting(own_memory, home, capsys):
    _connected(home)

    assert cli.main(_argv("status", home)) == 0

    output = capsys.readouterr().out
    assert "takeover: off: fake install=fake-1" in output
    assert "not offered" not in output, "the option may be offered for this install"
    assert set_takeover(home, _INSTALL, True, sync_ok=_synced).state == "complete"

    assert cli.main(_argv("status", home)) == 0

    output = capsys.readouterr().out
    assert "takeover: on: fake install=fake-1" in output
    assert f"  setting: {own_memory.path} memory" in output
    assert "  original value: True" in output
    assert "  own memory setting now: False" in output
    assert "hooks are off" not in output, "the hooks are on: nothing to warn about"


def test_takeover_status_warns_when_the_hooks_went_off(own_memory, home, capsys):
    _connected(home)
    set_takeover(home, _INSTALL, True, sync_ok=_synced)
    install_module._write_record(home, replace(_record(home), hooks=False))

    assert cli.main(_argv("status", home)) == 0

    output = capsys.readouterr().out
    assert "takeover: on: fake install=fake-1" in output
    assert "hooks are off" in output and "takeover off fake" in output, (
        "nothing records this host any more: the way back is printed"
    )


def test_takeover_status_lists_connected_installs_and_narrows(own_memory, home, capsys):
    _connected(home, install_id="fake-1", profile="work")
    _connected(home, install_id="fake-2", profile="life")
    _connected(home, install_id="fake-3")
    install_module._write_record(home, replace(_record(home, "fake-3"), state=STATE_DISCONNECTED))

    assert cli.main(_argv("status", home)) == 0

    output = capsys.readouterr().out
    assert "install=fake-1" in output and "install=fake-2" in output
    assert "install=fake-3" not in output, "a disconnected install is not taken over any more"
    assert cli.main(_argv("status", home, "--profile", "life")) == 0
    narrowed = capsys.readouterr().out
    assert "install=fake-2" in narrowed and "install=fake-1" not in narrowed
    assert cli.main(_argv("status", home, "--install", "fake-1")) == 0
    narrowed = capsys.readouterr().out
    assert "install=fake-1" in narrowed and "install=fake-2" not in narrowed


def test_takeover_status_says_why_the_option_is_not_offered(own_memory, home, capsys):
    _connected(home, hooks=False)

    assert cli.main(_argv("status", home)) == 0

    output = capsys.readouterr().out
    assert "takeover: off: fake install=fake-1" in output
    assert f"  not offered: {takeover_blocker(_record(home))}" in output


def test_takeover_on_needs_yes_without_a_terminal(
    own_memory, home, service, monkeypatch, capsys
):
    _connected(home, sync=_grant(own_memory))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.main(_argv("on", home)) == 1

    output = capsys.readouterr().out
    assert "aborted: install=fake-1 takeover aborted; host configuration was not changed" in (
        output
    )
    assert "non-interactive: pass --yes to continue" in output
    assert own_memory.current(None) is True and _record(home).takeover == {}


def test_takeover_needs_an_initialized_home(tmp_path, capsys):
    data_home = MemoryHome(tmp_path / "memory")

    assert cli.main(["takeover", "status", "codex", "--home", str(data_home.root)]) == 1

    assert "run `jiuwen-memory init`" in capsys.readouterr().out
    assert not data_home.root.exists(), (
        "nothing is written into an uninitialized data directory"
    )


def test_takeover_needs_a_connected_install(own_memory, home, capsys):
    for action in ("on", "off", "status"):
        assert cli.main(_argv(action, home, "--yes")) == 1, action
        assert f"no connected install for {_HOST}" in capsys.readouterr().err


def test_disconnect_resolves_pending_sync(own_memory, home):
    _connected(home)
    save_scan_grant(home, replace(ScanGrant(allowed=True), pending_sync=(_INSTALL, "fake-9")))
    before = load_scan_grant(home)

    install_module.disconnect(home, _INSTALL)

    grant = load_scan_grant(home)
    assert grant.pending_sync == ("fake-9",), (
        "a disconnected install no longer waits for sync authorization"
    )
    assert grant.version == before.version + 1, (
        "only remembering the ignored instance is an authorization change; "
        "dropping the reminder is runtime state"
    )


def test_scan_status_lists_included_instances(home, capsys):
    key = instance_key(_HOST, {"config_root": "/homes/me/.fake"})
    argv = ["scan", "status", "--home", str(home.root)]
    save_scan_grant(home, replace(ScanGrant(allowed=True), included=(key,)))

    assert cli.main(argv) == 0

    assert f"included again: {key}" in capsys.readouterr().out
    save_scan_grant(home, ScanGrant(allowed=True))
    assert cli.main(argv) == 0
    assert "included again: none" in capsys.readouterr().out
