# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP 启动回归：真实 stdio 子进程与 Streamable HTTP 协议握手。"""

import asyncio
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("mcp.server.fastmcp")

pytestmark = pytest.mark.integration


@pytest.fixture
def mcp_entry(monkeypatch):
    monkeypatch.setenv("JIUWEN_MEMORY_MCP_AUTH_MODE", "required")
    monkeypatch.setattr(sys, "argv", ["mcp"])
    return importlib.import_module("jiuwen_memory_entry.mcp_server.__main__")


def test_streamable_http_initializes_and_lists_tools(monkeypatch, mcp_entry) -> None:
    from starlette.testclient import TestClient

    mcp_main = mcp_entry

    server = Mock(start_background=AsyncMock())
    monkeypatch.setattr(mcp_main, "_SRV", server)
    monkeypatch.setattr(mcp_main.mcp.settings, "json_response", True)
    monkeypatch.setattr(mcp_main.mcp, "_session_manager", None)
    app = mcp_main.mcp.streamable_http_app()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1:8138") as client:
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "startup-regression", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "agent-memory"
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        notification = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notification.status_code == 202, notification.text
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert response.status_code == 200, response.text
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "memory_add" in names
    server.start_background.assert_awaited_once_with()
    server.close.assert_called_once_with()

def test_stdio_subprocess_initializes_and_lists_tools() -> None:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[3]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jiuwen_memory_entry.mcp_server"],
        cwd=str(root),
        env={
            "PYTHONPATH": os.pathsep.join([str(root), str(root / "jiuwen_memory_entry/core")]),
            "JIUWEN_MEMORY_MCP_AUTH_MODE": "required",
            "MCP_TRANSPORT": "stdio",
        },
    )

    async def connect() -> None:
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "agent-memory"
                    tools = await session.list_tools()
                    assert "memory_add" in {tool.name for tool in tools.tools}

    asyncio.run(connect())
