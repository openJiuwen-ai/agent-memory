"""MCP bridge local discovery (F17 Task 14)."""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import pytest

from jiuwen_memory_entry.local import discovery as discovery_module
from jiuwen_memory_entry.local.home import MemoryHome

pytestmark = pytest.mark.unit


def test_local_mode_reads_install_token_and_port(tmp_path, monkeypatch):
    home = MemoryHome(tmp_path / "home")
    install = home.installs_dir / "codex-x"
    install.mkdir(parents=True)
    (install / "token").write_text("token-x\n", encoding="utf-8")
    monkeypatch.setenv("JIUWEN_MEMORY_HOME", str(home.root))
    monkeypatch.delenv("AGENT_MEMORY_URL", raising=False)
    monkeypatch.setenv("JIUWEN_MEMORY_INSTALL", "codex-x")
    discovery = discovery_module.Discovery(
        pid=1, port=4321, protocol=1, instance_nonce="n", home=str(home), started_at=0.0
    )
    monkeypatch.setattr(discovery_module, "ensure_service", lambda *a, **k: discovery)
    import jiuwen_memory_adapter.continuity.mcp as mcp

    client, mode, local = mcp._connect()
    assert mode == "local"
    assert local is not None
    assert local.base_url == "http://127.0.0.1:4321"
    assert local.token == "token-x"
    client.close()


def test_local_mode_requires_install(tmp_path, monkeypatch):
    home = MemoryHome(tmp_path / "home")
    monkeypatch.setenv("JIUWEN_MEMORY_HOME", str(home.root))
    monkeypatch.delenv("AGENT_MEMORY_URL", raising=False)
    monkeypatch.delenv("JIUWEN_MEMORY_INSTALL", raising=False)
    import jiuwen_memory_adapter.continuity.mcp as mcp

    with pytest.raises(RuntimeError):
        mcp._connect()
