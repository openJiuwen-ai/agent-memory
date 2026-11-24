"""Verify authenticated bidirectional message policy and native recipient receipts."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("openjiuwen.harness.factory")

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.foundation.tool.schema import ToolTimeoutResult
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.subagent.subagent_rail import SubagentRail
from openjiuwen.harness.tools.subagent import TaskTool

from jiuwen_memory_adapter.agent_security._validation import has_exact_type
from jiuwen_memory_adapter.agent_security.frameworks import AgentRunContext
from jiuwen_memory_adapter.agent_security.hooks import Hook
from jiuwen_memory_adapter.agent_security.runtime import ApprovalChoice, SecurityDenied
from jiuwen_memory_adapter.jiuwenswarm.security_messages import MEMORY_PROVIDER, SwarmMessageGate
from jiuwen_memory_adapter.jiuwenswarm.security_rail import JiuwenSecurityRail

from ..agent_security.fixtures import SAMPLE, sdk_security
from ..agent_security.pipeline_fixtures import bundle

pytestmark = pytest.mark.integration


def test_swarm_rail_projects_native_tool_failure():
    actor = object()

    def ensure(current_actor, *, hook, producer, operation_id, value):
        assert current_actor is actor and hook == Hook.AFTER_TOOL_CALL
        assert producer == "subagent_spawn" and operation_id == "round:tool-projection"
        assert value["error"] == f"工具失败 {SAMPLE}"
        return {**value, "error": "工具失败 [PRIVATE]"}

    rail = JiuwenSecurityRail(
        SimpleNamespace(ensure=ensure),
        admit_model=None, admit_tool=None,
        current=lambda: AgentRunContext(actor, "round"),
    )
    inputs = SimpleNamespace(
        tool_msg=None, tool_name="subagent_spawn",
        tool_result=ToolTimeoutResult(success=False, data=None, error=f"工具失败 {SAMPLE}"),
    )
    asyncio.run(rail.after_tool_call(SimpleNamespace(inputs=inputs)))
    assert has_exact_type(inputs.tool_result, ToolTimeoutResult)
    assert inputs.tool_result.error == "工具失败 [PRIVATE]"


def test_swarm_task_rejects_rewritten_child_input():
    owner = object()
    plugin = SimpleNamespace(
        current_run=lambda: AgentRunContext(owner, "round"),
        sources=SimpleNamespace(snapshot=lambda _: SimpleNamespace(
            references=(SimpleNamespace(provider=MEMORY_PROVIDER),),
        )),
    )
    gate = SwarmMessageGate(plugin, owner)
    received = []

    class NativeChild:
        async def stream(self, inputs, **kwargs):
            received.append(inputs)
            yield None

    rail = SubagentRail()
    native = SimpleNamespace(
        configured_rails=lambda: [rail],
        create_subagent=lambda *_: NativeChild(),
    )
    card = ToolCard(id=uuid4().hex, name="test_task_tool")
    tool = TaskTool(card, native)
    native.ability_manager = SimpleNamespace(get=lambda _: card)
    rail.tools = [tool]

    async def native_invoke(inputs, **kwargs):
        assert inputs.get("task_description", "").startswith("MindVault task ")
        child = native.create_subagent("general-purpose", "native-child-session")
        async for _ in child.stream({
            "conversation_id": "native-child-session", "query": "rewritten",
        }):
            pytest.fail("Rewritten input produced a child result")

    async def configure(*_):
        pytest.fail("Rewritten input reached child configuration")

    tool.invoke = native_invoke
    Runner.resource_mgr.add_tool(tool)
    try:
        assert gate.bind_native(native, None, configure=configure) == (tool,)
        args = {"subagent_type": "general-purpose", "task_description": SAMPLE}
        with pytest.raises(SecurityDenied, match="任务参数.*被改写"):
            asyncio.run(tool.invoke(args))
        assert not received
    finally:
        Runner.resource_mgr.remove_tool(card.id)


@pytest.mark.parametrize("choice", list(ApprovalChoice))
def test_swarm_message_gate_protects_both_directions(sdk_source, choice):
    plugin, initial, *_ = bundle(
        sdk_source, "http://127.0.0.1:1", "openjiuwen", choice, interactive=True,
        register_memory_tools=False,
    )
    owner = plugin.new_session(replace(initial, agent_id="main"))
    runtime, _, _, _, _, scope, _ = sdk_source
    unit, = runtime.api.add(f"联络备忘 {SAMPLE}", scope, security=sdk_security(scope))
    ordinary, = runtime.api.add("周五整理项目文档。", scope, security=sdk_security(scope))
    memory = plugin.connect_memory_backend(
        api=runtime.api, provider=MEMORY_PROVIDER, scope_for=lambda _: scope,
        security_for=lambda _: sdk_security(scope), authorize_use=lambda *args: True,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    gate = SwarmMessageGate(plugin, owner, child_id="worker")
    child = gate.child("native-child-session", fresh=True)

    async def one(source, target, selected):
        received = []
        operation = asyncio.create_task(gate.send(
            AgentRunContext(source, "round"), target, f"联络备忘 {SAMPLE}",
            deliver=received.append,
        ))
        async with asyncio.timeout(5):
            requests = plugin.pending(owner)
            while not requests:
                assert not operation.done()
                await asyncio.sleep(.01)
                requests = plugin.pending(owner)
        pending, = requests
        assert pending["target"] == f"agent:{target.agent_id}" and not received
        plugin.decide(
            actor=owner, approval_id=pending["approval_id"], version=pending["version"],
            choice=selected,
        )
        if selected == ApprovalChoice.ABORT:
            with pytest.raises(SecurityDenied):
                await operation
            assert not received
        else:
            await operation
            value = SAMPLE if selected == ApprovalChoice.CONTINUE_ORIGINAL else "[PRIVATE]"
            assert received == [f"联络备忘 {value}"]

    async def check():
        try:
            with pytest.raises(SecurityDenied):
                gate.child("unknown-restored-session", fresh=False)
            await memory.reader.aget(ordinary.id, owner)
            received = []
            await gate.send(
                AgentRunContext(owner, "ordinary"), child, "周五整理项目文档。",
                deliver=received.append,
            )
            assert received == ["周五整理项目文档。"] and not plugin.pending(owner)
            await memory.reader.aget(unit.id, owner)
            await one(owner, child, ApprovalChoice.ABORT)
            assert plugin.sources.snapshot(child).level == "S1"
            await one(owner, child, ApprovalChoice.SEND_REDACTED)
            assert gate.has_memory(child)
            await one(child, owner, choice)
        finally:
            await plugin.aclose()

    asyncio.run(check())
