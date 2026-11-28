"""WorkBuddy detection and effective-entry verification."""

from __future__ import annotations

import hashlib
import json

import pytest

from jiuwen_memory_entry.local.hosts import HostInstall
from jiuwen_memory_entry.local.hosts.workbuddy import (
    WorkBuddyMcpWriter,
    detect_workbuddy,
    effective_server,
)

pytestmark = pytest.mark.unit


def _server_name(install_id: str) -> str:
    return "jiuwen-memory-" + hashlib.sha256(install_id.encode("utf-8")).hexdigest()[:8]


def test_absent_does_not_create_files(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_WORKBUDDY_HOME", str(tmp_path / "missing"))
    monkeypatch.setenv("JIUWEN_CODEBUDDY_HOME", str(tmp_path / "missing2"))
    with pytest.raises(ValueError, match="not detected"):
        WorkBuddyMcpWriter().merge(HostInstall("abcdef123", "workbuddy", "c", "p"))
    assert list(tmp_path.iterdir()) == []


def test_community_creates_then_rolls_back_file(tmp_path, monkeypatch):
    home = tmp_path / "workbuddy"
    home.mkdir()
    monkeypatch.setenv("JIUWEN_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("JIUWEN_CODEBUDDY_HOME", str(tmp_path / "none"))
    assert detect_workbuddy().state == "no_config"
    writer = WorkBuddyMcpWriter()
    install = HostInstall("abcdef123456", "workbuddy", "c", "p")
    entries = writer.merge(install)
    assert writer.verify(install)
    assert entries[0].created_file
    servers = json.loads((home / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == [_server_name("abcdef123456")]
    writer.rollback(install, entries)
    assert not (home / "mcp.json").exists()


def test_enterprise_project_entry_overrides_user_and_verify_fails(tmp_path, monkeypatch):
    user = tmp_path / "codebuddy"
    user.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JIUWEN_CODEBUDDY_HOME", str(user))
    monkeypatch.setenv("JIUWEN_WORKBUDDY_HOME", str(tmp_path / "none"))
    install = HostInstall(
        "abcdef123456", "workbuddy", "c", "p", edition="enterprise", scope="user",
        project_root=str(project),
    )
    writer = WorkBuddyMcpWriter()
    entries = writer.merge(install)
    detection = detect_workbuddy(project)
    name = _server_name("abcdef123456")
    (project / ".codebuddy").mkdir()
    (project / ".codebuddy" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {name: {
            "command": "other", "env": {"JIUWEN_MEMORY_INSTALL": "other"}
        }}}),
        encoding="utf-8",
    )
    assert effective_server(detection, name)[0] == "project"
    assert not writer.verify(install)
    writer.rollback(install, entries)


def test_broken_mcp_json_is_never_overwritten(tmp_path, monkeypatch):
    home = tmp_path / "workbuddy"
    home.mkdir()
    monkeypatch.setenv("JIUWEN_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("JIUWEN_CODEBUDDY_HOME", str(tmp_path / "none"))
    path = home / "mcp.json"
    broken = '{"mcpServers": {'
    path.write_text(broken, encoding="utf-8")
    writer = WorkBuddyMcpWriter()
    install = HostInstall("abcdef123456", "workbuddy", "c", "p")
    with pytest.raises(ValueError, match="invalid JSON"):
        writer.merge(install)
    assert path.read_text(encoding="utf-8") == broken
