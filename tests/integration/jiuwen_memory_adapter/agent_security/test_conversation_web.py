"""在原宿主聊天界面挂载审批组件；实际 agent 运行等待时控制请求独立到达。"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from jiuwen_memory_adapter.agent_security import ApprovalChoice, create_chat_app

from .fixtures import SAMPLE
from .pipeline_fixtures import bundle, responder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("framework", ["openjiuwen"])
@pytest.mark.parametrize("choice", list(ApprovalChoice))
def test_same_conversation_http_approvals(llm_server, sdk_source, framework, choice):
    pytest.importorskip("openjiuwen")
    root, server = llm_server
    server["responder"] = responder
    plugin, actor, model, memory, tool, shown, sent = bundle(
        sdk_source, root, framework, choice, interactive=True
    )
    owner = plugin.new_session(replace(actor, agent_id="main"))

    async def run():
        if framework == "openjiuwen":
            from openjiuwen.core.runner import Runner

            await Runner.start()
        try:
            child = await plugin.agent(agent_id="worker", model=model)
            delegation = plugin.delegate_tool(child, name="delegate_worker")
            agent = await plugin.agent(
                agent_id="main", model=model, tools=[memory, tool, delegation]
            )
            app = create_chat_app(
                plugin,
                agent,
                allowed_origins=["http://app.test"],
                authenticate=lambda scope: (
                    owner if (b"cookie", b"owner=valid") in scope["headers"] else None
                ),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://app.test",
                headers={"origin": "http://app.test", "cookie": "owner=valid"},
            ) as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/security/widget.mjs")).status_code == 200
                admitted = await client.post("/run", json={"text": "pipeline-job"})
                assert admitted.status_code == 200
                run_id = admitted.json()["run_id"]
                decisions, crossed = [], False
                for _ in range(1200):
                    response = await client.get("/security/requests")
                    assert response.status_code == 200
                    for request in response.json()["requests"]:
                        if request["approval_id"] in decisions:
                            continue
                        assert SAMPLE not in json.dumps(request)
                        body = {
                            "approval_id": request["approval_id"],
                            "version": request["version"],
                            "choice": str(
                                choice
                                if request["action"] in {"send_report", "agent.response"}
                                else ApprovalChoice.SEND_REDACTED
                            ),
                        }
                        if not crossed:
                            assert (
                                await client.post(
                                    "/security/decide",
                                    json=body,
                                    headers={"origin": "http://attacker.test"},
                                )
                            ).status_code == 403
                            assert (
                                await client.post(
                                    "/security/decide",
                                    json=body,
                                    headers={"cookie": "owner=invalid"},
                                )
                            ).status_code == 403
                            assert (
                                await client.post(
                                    "/security/decide", json={**body, "body": "injected"}
                                )
                            ).status_code == 403
                            crossed = True
                        assert (await client.post("/security/decide", json=body)).status_code == 200
                        decisions.append(request["approval_id"])
                    result = (await client.post("/result", json={"run_id": run_id})).json()
                    if result["status"] != "running":
                        break
                    await asyncio.sleep(0.01)
                assert result["status"] == (
                    "stopped" if choice == ApprovalChoice.ABORT else "completed"
                )
                assert decisions and crossed
                if choice != ApprovalChoice.ABORT:
                    assert result["content"] == "final [PRIVATE]"
                assert SAMPLE not in json.dumps(result)
                assert "13800138000" not in json.dumps(server["calls"])
                events = (await client.get("/security/audit")).json()["events"]
                assert events and SAMPLE not in json.dumps(events)
        finally:
            await plugin.aclose()
            if framework == "openjiuwen":
                await Runner.stop()

    asyncio.run(run())
