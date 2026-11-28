"""Hermes profile isolation and provider restoration."""

from __future__ import annotations

import pytest
import yaml

from jiuwen_memory_entry.local.hosts import HostInstall
from jiuwen_memory_entry.local.hosts.hermes import HermesHookWriter, HermesMcpWriter

pytestmark = pytest.mark.unit


def test_profiles_have_independent_mcp_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_HERMES_HOME", str(tmp_path))
    writer = HermesMcpWriter()
    one = HostInstall("install-one", "hermes", "c", "p", profile="work")
    two = HostInstall("install-two", "hermes", "c", "p", profile="life")
    writer.merge(one)
    writer.merge(two)
    first = yaml.safe_load((tmp_path / "profiles/work/config.yaml").read_text())
    second = yaml.safe_load((tmp_path / "profiles/life/config.yaml").read_text())
    assert first["mcp_servers"]["jiuwen-memory"]["env"]["JIUWEN_MEMORY_INSTALL"] == "install-one"
    assert second["mcp_servers"]["jiuwen-memory"]["env"]["JIUWEN_MEMORY_INSTALL"] == "install-two"


def test_hook_replaces_and_restores_previous_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWEN_HERMES_HOME", str(tmp_path))
    path = tmp_path / "profiles/work/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("memory:\n  provider: mem0\n", encoding="utf-8")
    writer = HermesHookWriter()
    install = HostInstall("install-one", "hermes", "c", "p", profile="work")
    assert "mem0" in "\n".join(writer.preflight(install))
    entries = writer.merge(install)
    assert writer.verify(install)
    assert yaml.safe_load(path.read_text())["memory"]["provider"] == "jiuwen_memory"
    writer.rollback(install, entries)
    assert yaml.safe_load(path.read_text())["memory"]["provider"] == "mem0"
    assert not (tmp_path / "plugins" / "jiuwen_memory").exists()
