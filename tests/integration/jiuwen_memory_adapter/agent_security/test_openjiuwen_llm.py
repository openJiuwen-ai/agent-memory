"""真实 Runner/ReActAgent + 公开合作 provider + OpenAI SDK，每次实际 HTTP 独立校验。"""

import asyncio
from urllib.parse import urljoin

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwen_memory_adapter.agent_security import Actor, ApprovalChoice, LlmTarget
from jiuwen_memory_adapter.agent_security.llm import LlmCallContext, create_llm_adapter
from jiuwen_memory_adapter.agent_security.sources import SourceLedger

from .fixtures import SAMPLE, llm_guard

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize("mode", ["dynamic", "fallback", "unknown_fallback"])
def test_native_dynamic_model_retry_and_fallback_target(llm_server, choice, mode):
    root, server = llm_server
    actor = Actor("tenant", "alice", "llm-thread", "agent", "1")
    sources, shown = SourceLedger(), []
    sources.begin(actor, fresh=True)
    guard = llm_guard({"target-a", "target-b"})

    def present(public):
        shown.append(public)
        guard.broker.submit(public["approval_id"], public["version"], choice, reviewer=actor)

    adapter = create_llm_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        targets=[
            LlmTarget("target-a", "provider-a", "model-a", urljoin(root, "/a/v1/chat/completions")),
            LlmTarget("target-b", "provider-b", "model-b", urljoin(root, "/b/v1/chat/completions")),
        ],
        context_for=lambda ctx: LlmCallContext(actor, "native-model-turn"),
        present=present,
    )
    provider = adapter.register_native_provider()

    def model(name, path):
        return Model(
            model_client_config=ModelClientConfig(
                client_provider=provider,
                api_key="dummy-auth-secret",
                api_base=urljoin(f"{root}{path}/", "v1"),
                max_retries=1,
            ),
            model_config=ModelRequestConfig(model=name),
        )

    async def scenario():
        await Runner.start()
        try:
            agent = ReActAgent(card=AgentCard(id=actor.agent_id))
            configuration = ReActAgentConfig()
            configuration.configure_prompt_template([{"role": "system", "content": "Local test"}])
            agent.configure(configuration)
            agent.set_llm(model("model-a", "/a"))
            backup = model("unregistered-model" if mode == "unknown_fallback" else "model-b", "/b")

            class RoutingRail(AgentRail):
                async def before_model_call(self, ctx):
                    if mode == "dynamic":
                        ctx.agent.set_llm(backup)

                async def on_model_exception(self, ctx):
                    if mode != "dynamic" and ctx.retry_attempt == 0:
                        ctx.agent.set_llm(backup)
                        ctx.request_retry()

            if mode != "dynamic":
                server["failures"]["/a/v1/chat/completions"] = 100
            await agent.register_rail(adapter.create_binding())
            await agent.register_rail(RoutingRail())
            if choice == ApprovalChoice.ABORT or mode == "unknown_fallback":
                try:
                    await Runner.run_agent(
                        agent=agent, inputs={"query": SAMPLE, "conversation_id": actor.session_id}
                    )
                except Exception as error:
                    assert SAMPLE not in str(error), "安全停止异常不能回显敏感载荷"
            else:
                result = await Runner.run_agent(
                    agent=agent, inputs={"query": SAMPLE, "conversation_id": actor.session_id}
                )
                assert result["result_type"] == "answer"
            if choice == ApprovalChoice.ABORT:
                assert server["calls"] == [] and len(shown) == 1
            else:
                expected_count = 1 if mode == "dynamic" else 2 if mode == "unknown_fallback" else 3
                assert len(server["calls"]) == len(shown) == expected_count
                expected = SAMPLE if choice == ApprovalChoice.CONTINUE_ORIGINAL else "[PRIVATE]"
                for call, public in zip(server["calls"], shown, strict=True):
                    assert call["body"]["messages"][-1]["content"] == expected
                    assert public["llm"]["endpoint"] == root + call["path"]
                    assert public["llm"]["model"] == call["body"]["model"]
                assert [p["llm"]["attempt"] for p in shown] == list(range(1, expected_count + 1))
        finally:
            await Runner.stop()

    try:
        asyncio.run(scenario())
    finally:
        adapter.close()
        sources.close()
        guard.broker.close()
