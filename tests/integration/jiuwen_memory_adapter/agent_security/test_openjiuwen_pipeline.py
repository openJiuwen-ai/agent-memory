"""真实 Runner 全链路及原生工具中断统一回到同一审批收件箱。"""

import asyncio
import json
from dataclasses import replace

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.runner import Runner

from jiuwen_memory_adapter.agent_security import ApprovalChoice, Hook, SecurityDenied

from .fixtures import SAMPLE
from .pipeline_fixtures import bundle, responder

pytestmark = pytest.mark.integration


def test_runner_uses_shared_strict_policy(llm_server, sdk_source):
    root, server = llm_server
    server["responder"] = responder
    plugin, actor, model, memory, tool, shown, sent = bundle(
        sdk_source,
        root,
        "openjiuwen",
        ApprovalChoice.SEND_REDACTED,
        redact_only=True,
    )
    session = plugin.new_session(replace(actor, agent_id="main"))

    async def run():
        await Runner.start()
        try:
            child = await plugin.agent(agent_id="worker", model=model)
            delegate = plugin.delegate_tool(child, name="delegate_worker")
            parent = await plugin.agent(
                agent_id="main", model=model, tools=[memory, tool, delegate]
            )
            return await parent.ainvoke("pipeline-job", session=session)
        finally:
            await plugin.aclose()
            await Runner.stop()

    assert asyncio.run(run()) == {"content": "final [PRIVATE]"}
    assert sent == [{"body": "ordinary derived summary", "to": "approved"}]
    assert shown and all(
        "CONTINUE_ORIGINAL" not in {option["value"] for option in request["options"]}
        for request in shown
    )


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize("streaming", [False, True])
def test_managed_runner_all_boundaries(llm_server, sdk_source, choice, streaming):
    root, server = llm_server
    server["responder"] = responder
    plugin, actor, model, memory, tool, shown, sent = bundle(sdk_source, root, "openjiuwen", choice)
    session = plugin.new_session(replace(actor, agent_id="main"))
    outputs = []

    async def run():
        await Runner.start()
        try:
            child = await plugin.agent(agent_id="worker", model=model)
            delegate = plugin.delegate_tool(child, name="delegate_worker")
            parent = await plugin.agent(
                agent_id="main", model=model, tools=[memory, tool, delegate]
            )
            plugin.coverage().require(set(Hook))
            if streaming:
                async for frame in parent.astream("pipeline-job", session=session):
                    outputs.append(frame)
            else:
                outputs.append(await parent.ainvoke("pipeline-job", session=session))
        finally:
            await plugin.aclose()
            await Runner.stop()

    if choice == ApprovalChoice.ABORT:
        with pytest.raises(SecurityDenied):
            asyncio.run(run())
        assert not sent and not outputs
    else:
        asyncio.run(run())
        assert outputs == [{"content": "final [PRIVATE]"}]
        assert sent == [{"body": "ordinary derived summary", "to": "approved"}], [
            (request["action"], request["level"]) for request in shown
        ]
    assert len([r for r in shown if r["action"] == "agent.message"]) >= 2
    assert any(r["action"] == "send_report" for r in shown)
    assert any(r["action"] == "agent.response" for r in shown)
    assert SAMPLE not in json.dumps(shown)
    assert "13800138000" not in json.dumps(server["calls"])
    assert "result-secret-value" not in json.dumps(server["calls"])
    assert not plugin.coverage().protected


def test_later_result_rails_rechecked_before_framework_consumption(llm_server, sdk_source):
    from openjiuwen.core.single_agent.rail.base import AgentRail

    class Rewrite(AgentRail):
        async def after_model_call(self, ctx):
            if ctx.inputs.response is not None and not ctx.inputs.response.tool_calls:
                ctx.inputs.response.content = "injected " + SAMPLE

        async def after_tool_call(self, ctx):
            if ctx.inputs.tool_name == "send_report":
                ctx.inputs.tool_msg.content = "tool injected " + SAMPLE
                ctx.inputs.tool_result = {"body": SAMPLE}

    root, server = llm_server
    server["responder"] = responder
    plugin, actor, model, memory, tool, _, _ = bundle(
        sdk_source, root, "openjiuwen", ApprovalChoice.SEND_REDACTED
    )
    session = plugin.new_session(replace(actor, agent_id="main"))

    async def run():
        await Runner.start()
        try:
            child = await plugin.agent(agent_id="worker", model=model)
            delegate = plugin.delegate_tool(child, name="delegate_worker")
            parent = await plugin.agent(
                agent_id="main", model=model, tools=[memory, tool, delegate], rails=[Rewrite()]
            )
            assert await parent.ainvoke("pipeline-job", session=session) == {
                "content": "injected [PRIVATE]"
            }
            assert SAMPLE not in json.dumps(server["calls"])
        finally:
            await plugin.aclose()
            await Runner.stop()

    asyncio.run(run())
