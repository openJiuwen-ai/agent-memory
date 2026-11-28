"""``jiuwen-memory`` CLI skeleton: init and service lifecycle entry points."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import json

import pytest

from jiuwen_memory_entry.local import cli
from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local.home import MemoryHome

pytestmark = pytest.mark.unit


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
