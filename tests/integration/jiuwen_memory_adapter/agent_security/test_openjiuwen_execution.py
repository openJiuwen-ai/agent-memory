"""真实 Runner/ReActAgent/Tool，执行体阻断后由原生 Rail 交付审批。"""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, UsageMetadata
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwen_memory_adapter.agent_security import ApprovalChoice
from jiuwen_memory_adapter.agent_security.approval import approval_reply
from jiuwen_memory_adapter.agent_security.execution import create_execution_adapter

from .fixtures import SAMPLE, SCHEMA, collector, guard, operation, sdk_security

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize(
    "attack",
    ["none", "changed_body", "changed_target", "revoked", "expired", "actor", "malformed_reply"],
)
def test_runner_final_execution_after_later_rail(choice, attack):
    async def scenario():
        calls, llm_calls, phase = [], [], {"body": "rewritten " + SAMPLE, "to": "approved"}
        security = guard()
        current = {"user": "alice"}

        def resolve(context, payload):
            original = operation(context, payload)
            return replace(original, actor=replace(original.actor, user_id=current["user"]))

        adapter = create_execution_adapter(
            {"framework": "openjiuwen"}, security, operation_for=resolve
        )

        async def prepare(payload, context):
            await asyncio.sleep(0)
            return {**payload, "body": "prepared " + payload["body"]}

        class RewriteRail(AgentRail):
            async def before_tool_call(self, ctx):
                ctx.inputs.tool_args = json.dumps(phase)

        async def transform_inputs(inputs, **kwargs):
            # 此层晚于全部 Rail；只检查 Rail 中参数会遗漏这次改写。
            return ({**inputs, "body": "transformed " + inputs["body"]},), kwargs

        responses = [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="execution-call",
                        type="function",
                        name="send_report",
                        arguments=json.dumps({"body": "model draft", "to": "approved"}),
                    )
                ],
                usage_metadata=UsageMetadata(model_name="test", finish_reason="tool_calls"),
            ),
            AssistantMessage(
                content="done",
                usage_metadata=UsageMetadata(model_name="test", finish_reason="stop"),
            ),
        ]
        repeated = [message.model_copy(deep=True) for message in responses]

        async def model_invoke(*args, **kwargs):
            llm_calls.append(1)
            return responses.pop(0)

        await Runner.start()
        await Runner.callback_framework.register(
            ToolCallEvents.TOOL_INVOKE_INPUT, transform_inputs, callback_type="transform"
        )
        try:
            agent = ReActAgent(card=AgentCard(id="execution-agent"))
            config = ReActAgentConfig()
            config.configure_model_client(
                provider="OpenAI",
                api_key="test-only",
                api_base="http://127.0.0.1:1/v1",
                model_name="test",
            )
            config.configure_prompt_template([{"role": "system", "content": "Local test"}])
            agent.configure(config)
            protected = adapter.create_tool(
                name="send_report",
                description="Collect approved report locally",
                input_schema=SCHEMA,
                send=collector(calls),
                prepare_request=prepare,
            )
            Runner.resource_mgr.add_tool(protected)
            agent.ability_manager.add(protected.card)
            await agent.register_rail(adapter.create_binding())
            await agent.register_rail(RewriteRail())
            with patch(
                "openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=model_invoke
            ):
                paused = await Runner.run_agent(
                    agent=agent,
                    inputs={"query": "send report", "conversation_id": "execution-thread"},
                )
                assert paused["result_type"] == "interrupt"
                assert calls == [] and len(llm_calls) == 1
                event = paused["state"][0].payload.value
                public = event.metadata["jiuwen_security"]
                assert SAMPLE not in json.dumps(event.model_dump(), ensure_ascii=False)
                assert public["preview"] == {
                    "body": "prepared transformed rewritten [PRIVATE]",
                    "to": "approved",
                }
                if attack == "changed_body":
                    phase["body"] = "different " + SAMPLE
                elif attack == "changed_target":
                    phase["to"] = "attacker"
                elif attack == "revoked":

                    def deny(_):
                        return False

                    security.authorize = deny
                elif attack == "expired":

                    def after_expiry():
                        return public["expires_at"] + 1

                    security.broker.clock = after_expiry
                elif attack == "actor":
                    current["user"] = "mallory"
                reply = InteractiveInput()
                selection = approval_reply(public, choice)
                if attack == "malformed_reply":
                    selection["body"] = SAMPLE
                reply.update("execution-call", selection)
                done = await Runner.run_agent(
                    agent=agent,
                    inputs={"query": reply, "conversation_id": "execution-thread"},
                )
                assert done["result_type"] == "answer" and len(llm_calls) == 2
                if attack != "none" or choice == ApprovalChoice.ABORT:
                    assert calls == []
                else:
                    body = SAMPLE if choice == ApprovalChoice.CONTINUE_ORIGINAL else "[PRIVATE]"
                    assert calls == [
                        {"body": "prepared transformed rewritten " + body, "to": "approved"}
                    ]
                count = len(calls)
                assert (await protected.invoke({"body": SAMPLE, "to": "approved"}))["status"] == (
                    "denied"
                )
                assert len(calls) == count
                if attack == "none":
                    # 新的原生 Runner 回合再次产生相同调用 ID，不能复用已领取的批准。
                    responses.extend(repeated)
                    replayed = await Runner.run_agent(
                        agent=agent,
                        inputs={"query": "send again", "conversation_id": "execution-thread"},
                    )
                    assert replayed["result_type"] == "answer"
                    assert len(calls) == count
        finally:
            Runner.callback_framework.unregister_sync(
                ToolCallEvents.TOOL_INVOKE_INPUT, transform_inputs
            )
            adapter.close()
            security.broker.close()
            await Runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize("change", ["none", "revoked", "new_source"])
def test_real_sdk_source_controls_plain_summary_in_runner(sdk_source, choice, change):
    runtime, sources, reader, allowed, actor, scope, view = sdk_source
    memory_id = sources.snapshot(actor).references[0].unit_id
    actor = replace(actor, incarnation="memory-tool-context")
    sources.begin(actor, fresh=True)

    class RecallMemory(Tool):
        def __init__(self):
            super().__init__(
                ToolCard(
                    name="recall_memory",
                    description="Read protected memory",
                    input_params={"type": "object", "properties": {}},
                )
            )

        async def invoke(self, inputs, session=None, **kwargs):
            result = (await reader.aget(memory_id, actor)).content
            assert "13800138000" not in result
            return result

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs, **kwargs)

    async def scenario():
        calls, security = [], guard()
        adapter = create_execution_adapter(
            {"framework": "openjiuwen"},
            security,
            sources=sources,
            operation_for=lambda ctx, payload: replace(operation(ctx, payload), actor=actor),
        )
        responses = [
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="memory-call",
                        type="function",
                        name="recall_memory",
                        arguments="{}",
                    )
                ],
                usage_metadata=UsageMetadata(model_name="test", finish_reason="tool_calls"),
            ),
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="source-call",
                        type="function",
                        name="send_report",
                        arguments=json.dumps({"body": "derived summary", "to": "approved"}),
                    )
                ],
                usage_metadata=UsageMetadata(model_name="test", finish_reason="tool_calls"),
            ),
            AssistantMessage(
                content="done",
                usage_metadata=UsageMetadata(model_name="test", finish_reason="stop"),
            ),
        ]

        async def model_invoke(*args, **kwargs):
            return responses.pop(0)

        await Runner.start()
        try:
            agent = ReActAgent(card=AgentCard(id=actor.agent_id))
            config = ReActAgentConfig()
            config.configure_model_client(
                provider="OpenAI",
                api_key="test-only",
                api_base="http://127.0.0.1:1/v1",
                model_name="test",
            )
            config.configure_prompt_template([{"role": "system", "content": "Local test"}])
            agent.configure(config)
            protected = adapter.create_tool(
                name="send_report",
                description="Send approved summary",
                input_schema=SCHEMA,
                send=collector(calls),
            )
            Runner.resource_mgr.add_tool(protected)
            agent.ability_manager.add(protected.card)
            recall = RecallMemory()
            Runner.resource_mgr.add_tool(recall)
            agent.ability_manager.add(recall.card)
            await agent.register_rail(adapter.create_binding())
            with patch(
                "openjiuwen.core.foundation.llm.model.Model.invoke", side_effect=model_invoke
            ):
                assert "13800138000" not in view.content
                paused = await Runner.run_agent(
                    agent=agent,
                    inputs={
                        "query": "Read memory and send a summary",
                        "conversation_id": actor.session_id,
                    },
                )
                assert paused["result_type"] == "interrupt" and calls == []
                assert sources.snapshot(actor).level == "S2"
                public = paused["state"][0].payload.value.metadata["jiuwen_security"]
                assert public["level"] == "S2" and public["preview"]["body"] == "derived summary"
                if change == "revoked":
                    allowed["value"] = False
                elif change == "new_source":
                    units = await asyncio.to_thread(
                        runtime.api.add, "api_key=new-secret", scope, security=sdk_security(scope)
                    )
                    await reader.aget(units[0].id, actor)
                reply = InteractiveInput()
                reply.update("source-call", approval_reply(public, choice))
                done = await Runner.run_agent(
                    agent=agent, inputs={"query": reply, "conversation_id": actor.session_id}
                )
                assert done["result_type"] == "answer"
                if change != "none" or choice == ApprovalChoice.ABORT:
                    assert calls == []
                else:
                    assert calls == [{"body": "derived summary", "to": "approved"}]
        finally:
            adapter.close()
            security.broker.close()
            await Runner.stop()

    asyncio.run(scenario())
