"""DSH MCP writer only touches the selected profile and preserves other entries."""

from __future__ import annotations

import pytest
import yaml

from jiuwen_memory_entry.local.hosts import HostInstall
from jiuwen_memory_entry.local.hosts.dsh import DshMcpWriter

pytestmark = pytest.mark.unit


def test_mcp_entry_is_scoped_and_preserves_user_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_DSH_HOME", str(tmp_path))
    for name in ("desktop", "dsh-tui"):
        (tmp_path / "profiles" / name).mkdir(parents=True)
    desktop = tmp_path / "profiles/desktop/cordis.patch.yml"
    desktop.write_text(yaml.safe_dump([{"id": "user", "name": "user-plugin"}]), encoding="utf-8")
    writer = DshMcpWriter()
    install = HostInstall("abcdef123456", "dsh", "c", "p", profile="desktop")
    entries = writer.merge(install)
    data = yaml.safe_load(desktop.read_text())
    assert [item["id"] for item in data] == ["user", "jiuwen-memory-abcdef12"]
    assert not (tmp_path / "profiles/dsh-tui/cordis.patch.yml").exists()
    assert writer.verify(install)
    writer.rollback(install, entries)
    assert yaml.safe_load(desktop.read_text())[0]["id"] == "user"
