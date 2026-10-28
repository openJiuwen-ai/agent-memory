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


@pytest.mark.parametrize("session_count", [1, 2])
def test_streamable_http_initializes_and_lists_tools(
    monkeypatch, mcp_entry, session_count
) -> None:
    from starlette.testclient import TestClient

    mcp_main = mcp_entry

    config = mcp_main.with_local_dev_security(mcp_main.load_config([mcp_main.OFFLINE]))
    server = mcp_main.Server.build(config)
    monkeypatch.setattr(server, "start_background", AsyncMock(wraps=server.start_background))
    monkeypatch.setattr(server, "close", Mock(wraps=server.close))
    monkeypatch.setattr(mcp_main, "_SRV", server)
    monkeypatch.setattr(mcp_main, "_TRANSPORT", "http")
    monkeypatch.setattr(mcp_main.mcp.settings, "json_response", True)
    monkeypatch.setattr(mcp_main.mcp, "_session_manager", None)
    app = mcp_main.mcp.streamable_http_app()
    headers = {"Accept": "application/json, text/event-stream"}

    def initialize(client):
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
        session_headers = {**headers, "Mcp-Session-Id": response.headers["mcp-session-id"]}
        notification = client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notification.status_code == 202, notification.text
        return session_headers

    def list_tools(client, session_headers):
        response = client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert response.status_code == 200, response.text
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "memory_add" in names
        response = client.post(
            "/mcp",
            headers=session_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "memory_add",
                    "arguments": {
                        "content": "session remains usable",
                        "scope": {"org": "local", "user": "developer"},
                    },
                },
            },
        )
        assert response.status_code == 200, response.text
        assert not response.json()["result"].get("isError"), response.text

    with TestClient(app, base_url="http://127.0.0.1:8138") as client:
        # 应用启动即持有 runtime；无需等待第一个客户端连接。
        server.start_background.assert_awaited_once_with()
        sessions = [initialize(client) for _ in range(session_count)]
        server.start_background.assert_awaited_once_with()
        for session_headers in sessions:
            list_tools(client, session_headers)
        for session_headers in sessions:
            response = client.delete("/mcp", headers=session_headers)
            assert response.status_code == 200, response.text
            server.close.assert_not_called()
            # 第一个会话退出后，另一个已有会话仍正常工作。
            if session_headers is not sessions[-1]:
                list_tools(client, sessions[-1])
        # 所有会话结束后，新会话仍复用未关闭的 runtime。
        list_tools(client, initialize(client))
        server.start_background.assert_awaited_once_with()
        server.close.assert_not_called()
    server.start_background.assert_awaited_once_with()
    server.close.assert_called_once_with()


@pytest.mark.parametrize("fail_startup", [False, True])
def test_http_application_failure_closes_runtime(monkeypatch, mcp_entry, fail_startup) -> None:
    from starlette.testclient import TestClient

    startup_error = RuntimeError("application failed") if fail_startup else None
    server = Mock(start_background=AsyncMock(side_effect=startup_error))
    monkeypatch.setattr(mcp_entry, "_SRV", server)
    monkeypatch.setattr(mcp_entry.mcp, "_session_manager", None)
    app = mcp_entry.mcp.streamable_http_app()
    with pytest.raises(RuntimeError, match="application failed"):
        with TestClient(app):
            server.start_background.assert_awaited_once_with()
            server.close.assert_not_called()
            raise RuntimeError("application failed")
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
