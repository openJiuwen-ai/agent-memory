# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实 MCP stdio 会话：新建内存注册表使用 Root 引导，不伪装成普通用户 Key。"""

import asyncio
import os
import secrets
import sys

from _support import EXAMPLE_DIR
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = os.environ.copy()
    env["JIUWEN_MEMORY_MCP_AUTH_MODE"] = "required"
    env["MCP_TRANSPORT"] = "stdio"
    env["AGENT_MEMORY_ROOT_API_KEY"] = secrets.token_urlsafe(32)
    env["AGENT_MEMORY_API_KEY"] = env["AGENT_MEMORY_ROOT_API_KEY"]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jiuwen_memory_entry.mcp_server", str(EXAMPLE_DIR / "api-key.yml")],
        env=env,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            written = await session.call_tool(
                "memory_add",
                {
                    "content": "MCP Root local test",
                    "scope": {"org": "system", "user": "root"},
                },
            )
            assert not written.isError, written
            print("PASS MCP required: Root authenticated; memory_add succeeded")
            result = await session.call_tool(
                "memory_verify_audit",
                {
                    "after_sequence": 0,
                    "page_size": 1000,
                    "max_samples": 20,
                    "anchor_policy": "if_configured",
                },
            )
            assert not result.isError, result
            # 本例 api-key.yml 没有完整性 provider；成功调用不表示审计检查通过。
            assert any("unsupported" in getattr(part, "text", "") for part in result.content), (
                result
            )
            print("PASS MCP verify_audit: unsupported without provider")


if __name__ == "__main__":
    asyncio.run(main())
