"""Start the MCP bridge as a real subprocess and call a task tool (F17 review)."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from tests.integration.jiuwen_memory_entry.local.test_hook_roundtrip import (  # noqa: F401
    runtime,
)

pytestmark = pytest.mark.integration


def test_bridge_starts_and_serves_task_tools(request):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home, _discovery, _codex = request.getfixturevalue("runtime")
    environment = {
        **os.environ,
        "JIUWEN_MEMORY_HOME": str(home.root),
        "JIUWEN_MEMORY_INSTALL": "codex-hook",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jiuwen_memory_adapter.continuity.mcp"],
        env=environment,
    )

    async def scenario() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert "agent_memory_continuity_task" in names
                assert {"agent_memory_add", "agent_memory_search"} <= names
                result = await session.call_tool(
                    "agent_memory_continuity_task",
                    {
                        "scope": {"org": "local", "space": "hook-project"},
                        "request": {
                            "operation": "find",
                            "connector_id": "codex-conn",
                            "context_id": "s1",
                        },
                    },
                )
                text = "\n".join(
                    item.text for item in result.content if hasattr(item, "text")
                )
                failed = getattr(result, "isError", getattr(result, "is_error", False))
                assert not failed, text
                assert "task" in text, text

    asyncio.run(scenario())
