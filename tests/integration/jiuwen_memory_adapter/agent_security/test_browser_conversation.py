"""可选真实浏览器验收：原聊天组件的三按钮驱动真实框架与 SDK。"""

import asyncio
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from jiuwen_memory_adapter.agent_security import ApprovalChoice, create_chat_app

from .pipeline_fixtures import bundle, responder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("framework", ["openjiuwen"])
@pytest.mark.parametrize("choice", list(ApprovalChoice))
def test_browser_same_chat(llm_server, sdk_source, framework, choice):
    if not os.environ.get("MINDVAULT_PLAYWRIGHT") or not os.environ.get("MINDVAULT_BROWSER"):
        pytest.skip("需要显式指定隔离浏览器和 Playwright 路径")
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("openjiuwen")
    root, state = llm_server
    state["responder"] = responder
    plugin, actor, model, memory, tool, _, sent = bundle(
        sdk_source, root, framework, choice, interactive=True
    )
    owner = plugin.new_session(replace(actor, agent_id="main"))

    async def run():
        if framework == "openjiuwen":
            from openjiuwen.core.runner import Runner

            await Runner.start()
        server, serving = None, None
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        try:
            child = await plugin.agent(agent_id="worker", model=model)
            delegate = plugin.delegate_tool(child, name="delegate_worker")
            parent = await plugin.agent(
                agent_id="main", model=model, tools=[memory, tool, delegate]
            )
            app = create_chat_app(
                plugin,
                parent,
                allowed_origins=[url],
                authenticate=lambda scope: (
                    owner if (b"cookie", b"owner=valid") in scope["headers"] else None
                ),
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            process = await asyncio.create_subprocess_exec(
                "node",
                str(Path(__file__).with_name("browser-conversation.mjs")),
                url,
                str(choice),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert b'"passed":true' in stdout
            assert bool(sent) == (choice != ApprovalChoice.ABORT)
        finally:
            if server is not None:
                server.should_exit = True
            if serving is not None:
                await serving
            sock.close()
            await plugin.aclose()
            if framework == "openjiuwen":
                await Runner.stop()

    asyncio.run(run())
