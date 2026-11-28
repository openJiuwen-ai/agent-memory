"""Codex hook merge keeps other tools' entries and rolls back only ours."""

from __future__ import annotations

import json
import tomllib

import pytest

from jiuwen_memory_entry.local.hosts import HostInstall
from jiuwen_memory_entry.local.hosts.codex import CodexWriter

pytestmark = pytest.mark.unit

INSTALL = HostInstall(
    install_id="codex-1234567890ab",
    host="codex",
    connector_id="codex-1234567890ab",
    project="default",
)


def test_existing_hooks_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    path = tmp_path / "codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    events = (
        "UserPromptSubmit", "Stop", "PostToolUse", "SessionStart",
        "SessionEnd", "PreToolUse", "Notification", "SubagentStop",
    )
    existing = {
        "hooks": {
            event: [{"type": "command", "command": f"cc-status {index}"}]
            for index, event in enumerate(events)
        },
        "other": {"kept": True},
    }
    path.write_text(json.dumps(existing), encoding="utf-8")
    writer = CodexWriter()
    entries = writer.merge(INSTALL)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["other"] == {"kept": True}
    assert len(data["hooks"]["UserPromptSubmit"]) == 2
    for index, event in enumerate(data["hooks"]):
        if event in {"UserPromptSubmit", "Stop", "PostToolUse"}:
            continue
        assert data["hooks"][event][0]["command"].startswith("cc-status"), "entry lost"
    assert writer.verify(INSTALL)
    kept = writer.rollback(INSTALL, entries)
    assert kept == []
    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["hooks"]["UserPromptSubmit"]) == 1
    assert after["hooks"]["UserPromptSubmit"][0]["command"].startswith("cc-status")
    assert not writer.verify(INSTALL)


def test_rollback_reports_user_modified_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    writer = CodexWriter()
    entries = writer.merge(INSTALL)
    path = tmp_path / "codex" / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["Stop"][0]["command"] = "user edited this"
    path.write_text(json.dumps(data), encoding="utf-8")
    kept = writer.rollback(INSTALL, entries)
    assert kept, "edited entries must be reported as kept"
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["hooks"]["Stop"][0]["command"] == "user edited this"


def test_no_empty_post_tool_use_hook_is_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    CodexWriter().merge(INSTALL)
    data = json.loads((tmp_path / "codex" / "hooks.json").read_text(encoding="utf-8"))
    assert "PostToolUse" not in data["hooks"], "empty hook entries must not start a process"


def test_jiuwenswarm_verify_reads_the_written_config(tmp_path, monkeypatch):
    from jiuwen_memory_entry.local.hosts.jiuwenswarm import JiuwenSwarmWriter

    config = tmp_path / "swarm.yaml"
    monkeypatch.setenv("JIUWEN_SWARM_CONFIG", str(config))
    writer = JiuwenSwarmWriter()
    install = HostInstall("swarm-1", "jiuwenswarm", "swarm-1", "default")
    writer.merge(install)
    assert writer.verify(install) is True
    config.write_text("memory: {}\n", encoding="utf-8")
    assert writer.verify(install) is False


def test_mcp_server_is_written_to_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    writer = CodexWriter()
    writer.merge(INSTALL)
    config = tmp_path / "codex" / "config.toml"
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    server = data["mcp_servers"]["jiuwen-memory"]
    assert server["env"]["JIUWEN_MEMORY_INSTALL"] == INSTALL.install_id
    assert server["args"][:1] == ["-m"]
    assert writer.verify(INSTALL)


def test_mcp_config_merge_is_idempotent_and_preserves_other_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
    writer = CodexWriter()
    writer.merge(INSTALL)
    writer.merge(INSTALL)
    text = config.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.jiuwen-memory]") == 1
    data = tomllib.loads(text)
    assert data["mcp_servers"]["other"]["command"] == "other"


def test_mcp_config_rollback_removes_only_our_section(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
    writer = CodexWriter()
    entries = writer.merge(INSTALL)
    kept = writer.rollback(INSTALL, entries)
    assert kept == []
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert "jiuwen-memory" not in data.get("mcp_servers", {})
    assert data["mcp_servers"]["other"]["command"] == "other"
    assert not writer.verify(INSTALL)


def test_mcp_config_escapes_windows_paths_and_install_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    install = HostInstall(
        install_id='swarm-"quoted"',
        host="codex",
        connector_id="swarm-1",
        project="default",
        interpreter=r"C:\Users\a b\python.exe",
    )
    CodexWriter().merge(install)
    data = tomllib.loads((tmp_path / "codex" / "config.toml").read_text(encoding="utf-8"))
    server = data["mcp_servers"]["jiuwen-memory"]
    assert server["command"] == r"C:\Users\a b\python.exe"
    assert server["env"]["JIUWEN_MEMORY_INSTALL"] == 'swarm-"quoted"'
    assert CodexWriter().verify(install)


def test_crlf_config_is_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(
        b'[mcp_servers.jiuwen-memory]\r\ncommand = "old"\r\n'
    )
    writer = CodexWriter()
    writer.merge(INSTALL)
    writer.merge(INSTALL)
    text = config.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.jiuwen-memory]") == 1, text
    data = tomllib.loads(text)
    assert data["mcp_servers"]["jiuwen-memory"]["env"]["JIUWEN_MEMORY_INSTALL"] == (
        INSTALL.install_id
    )


def test_unparsable_hooks_file_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    path = tmp_path / "codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    broken = '{"hooks": {"Stop": [{"command": "other-tool"}]}'
    path.write_text(broken, encoding="utf-8")
    writer = CodexWriter()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        writer.merge(INSTALL)
    assert path.read_text(encoding="utf-8") == broken, "user hooks must stay untouched"
    assert not writer.verify(INSTALL)
    assert [entry.section for entry in writer.rollback(INSTALL, [])] == []


def test_hook_writer_verify_reports_false_on_a_broken_file(tmp_path, monkeypatch):
    from jiuwen_memory_entry.local.hosts.codex import CodexHookWriter

    monkeypatch.setenv("JIUWEN_CODEX_HOME", str(tmp_path / "codex"))
    path = tmp_path / "codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks":', encoding="utf-8")
    assert CodexHookWriter.verify(INSTALL) is False
