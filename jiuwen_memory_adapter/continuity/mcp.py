"""Remote-only stdio MCP bridge. Every product uses the same Registry service."""

import asyncio
import os

from jiuwen_memory_adapter.continuity import tool_schemas
from jiuwen_memory_adapter.continuity.client import ContinuityClient


async def run():
    import json

    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    client = ContinuityClient(os.environ["AGENT_MEMORY_URL"], os.environ["AGENT_MEMORY_API_KEY"])
    server = Server("agent-memory-continuity")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name=s["name"], description=s["description"], inputSchema=s["parameters"])
            for s in tool_schemas()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in {s["name"] for s in tool_schemas()}:
            raise ValueError("unknown tool")
        result = await asyncio.to_thread(
            client.call, name.removeprefix("agent_memory_"), arguments or {}
        )
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(run())
