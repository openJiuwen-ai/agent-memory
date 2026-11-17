"""真实 Runner 的 invoke/stream 最终交付和 agent 接收前来源继承。"""

import asyncio
import json
from dataclasses import replace
from urllib.parse import urljoin

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from jiuwen_memory_adapter.agent_security import (
    Actor,
    ApprovalChoice,
    Hook,
    LlmTarget,
    SecurityDenied,
)
from jiuwen_memory_adapter.agent_security.approval import ApprovalInbox
from jiuwen_memory_adapter.agent_security.delivery import DeliveryContext, create_delivery_adapter
from jiuwen_memory_adapter.agent_security.inspection import create_text_inspector
from jiuwen_memory_adapter.agent_security.llm import LlmCallContext, create_llm_adapter
from jiuwen_memory_adapter.agent_security.results import ResultGuard
from jiuwen_memory_adapter.agent_security.runtime import (
    ApprovalBroker,
    DefaultEgressPolicy,
    EgressGuard,
    PayloadInspector,
)
from jiuwen_memory_adapter.agent_security.sources import SourceLedger

from .fixtures import SAMPLE, llm_guard

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("choice", list(ApprovalChoice))
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("message", [False, True])
def test_runner_native_result_and_final_gate(llm_server, choice, streaming, message):
    root, server = llm_server
    actor = Actor("tenant", "alice", "native-final", "agent", "1")
    recipient = replace(actor, agent_id="child") if message else None
    sources = SourceLedger()
    sources.begin(actor, fresh=True)
    if recipient:
        sources.begin(recipient, fresh=True)
    inspector, broker, shown = create_text_inspector(), ApprovalBroker(), []
    results = ResultGuard(sources, inspector, authorize_input=lambda *args: True)
    inbox, guard_llm = ApprovalInbox(broker), llm_guard({"a"})
    llm = create_llm_adapter(
        {"framework": "openjiuwen"},
        guard_llm,
        sources=sources,
        results=results,
        targets=[LlmTarget("a", "provider-a", "model-a", urljoin(root, "/a/v1/chat/completions"))],
        context_for=lambda ctx: LlmCallContext(actor, "native-model-turn"),
        present=lambda public: pytest.fail("公开首轮请求不应审批"),
    )
    if streaming:
        events = []
        for piece, reason in [(SAMPLE[:8], None), (SAMPLE[8:], None), ("", "stop")]:
            events.append(
                "data: "
                + json.dumps(
                    {
                        "id": "chatcmpl-safe",
                        "created": 1,
                        "object": "chat.completion.chunk",
                        "model": "model-a",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": piece},
                                "finish_reason": reason,
                            }
                        ],
                    }
                )
                + "\n\n"
            )
        server["responses"]["/a/v1/chat/completions"] = {
            "type": "text/event-stream",
            "chunks": [*events, "data: [DONE]\n\n"],
        }
    else:
        server["responses"]["/a/v1/chat/completions"] = {
            "type": "application/json",
            "chunks": [
                json.dumps(
                    {
                        "id": "chatcmpl-safe",
                        "created": 1,
                        "object": "chat.completion",
                        "model": "model-a",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": SAMPLE},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
            ],
        }

    def present(public, *, actor):
        inbox.present(public, actor=actor)
        shown.extend(inbox.pending(actor))
        inbox.decide(
            actor=actor, approval_id=public["approval_id"], version=public["version"], choice=choice
        )

    guard = EgressGuard(
        PayloadInspector(inspector, frozenset({"body"})),
        DefaultEgressPolicy(frozenset({"recipient"})),
        broker,
        authorize=lambda _: True,
    )
    delivery = create_delivery_adapter(
        {"framework": "openjiuwen"},
        guard,
        sources=sources,
        context_for=lambda host, hook: DeliveryContext(
            actor, "final", "recipient", "reply", recipient
        ),
        present=present,
        authorize_transfer=lambda *args: True,
    )
    provider = llm.register_native_provider()
    released = []

    async def run():
        await Runner.start()
        try:
            agent = ReActAgent(card=AgentCard(id=actor.agent_id))
            configuration = ReActAgentConfig()
            configuration.configure_prompt_template([{"role": "system", "content": "Local test"}])
            agent.configure(configuration)
            agent.set_llm(
                Model(
                    model_client_config=ModelClientConfig(
                        client_provider=provider,
                        api_base=urljoin(root, "/a/v1"),
                        api_key="fixture-auth",
                        max_retries=0,
                    ),
                    model_config=ModelRequestConfig(model="model-a"),
                )
            )
            await agent.register_rail(llm.create_binding())
            binding = delivery.create_binding()
            inputs = {"query": "safe prompt", "conversation_id": actor.session_id}
            kwargs = {
                "host": object(),
                "hook": Hook.BEFORE_AGENT_MESSAGE if message else Hook.BEFORE_AGENT_RESPONSE,
            }
            if streaming:
                async for chunk in binding.astream(agent, inputs, **kwargs):
                    released.append(chunk)
            else:
                released.append(await binding.ainvoke(agent, inputs, **kwargs))
        finally:
            await Runner.stop()

    try:
        if choice == ApprovalChoice.ABORT:
            with pytest.raises(SecurityDenied):
                asyncio.run(run())
            assert not released
        else:
            asyncio.run(run())
            assert released == [{"content": "[PRIVATE]"}]
        assert len(shown) == 1 and shown[0]["level"] == "S2"
        if recipient:
            assert sources.snapshot(recipient).level == (
                "S1" if choice == ApprovalChoice.ABORT else "S2"
            )
    finally:
        delivery.close()
        llm.close()
        results.close()
        inbox.close()
        broker.close()
        guard_llm.broker.close()
        sources.close()
