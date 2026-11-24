"""Run native DeepAgent prefetch and a tool discovered after agent construction."""

import asyncio
import json
import sys
from contextlib import aclosing
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("openjiuwen.harness.factory")
pytest.importorskip("openjiuwen.harness.schema.interaction")
pytest.importorskip("mcp")
pytest.importorskip("jiuwenswarm")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import create_deep_agent

from jiuwen_memory_adapter.agent_security import ApprovalChoice
from jiuwen_memory_adapter.agent_security.frameworks import AgentRunContext
from jiuwen_memory_adapter.agent_security.runtime import SecurityDenied
from jiuwen_memory_adapter.jiuwenswarm import security_control as swarm_control
from jiuwen_memory_adapter.jiuwenswarm.security_binding import SwarmSessionBinding
from jiuwen_memory_adapter.jiuwenswarm.security_host import (
    SwarmSessionPorts,
    start_swarm_protection,
)

from ..agent_security.fixtures import SAMPLE, sdk_security
from ..agent_security.pipeline_fixtures import bundle

pytestmark = pytest.mark.integration

SYNTHETIC_PASSWORD = "SwarmFixtureOnly_42"
RECALL_MARKER = "credential-fixture"


@pytest.mark.parametrize("mode", [
    "invoke", "stream", "interaction", "work", "code",
])
@pytest.mark.parametrize("choice", list(ApprovalChoice))
def test_native_swarm_prefetch_and_dynamic_mcp(
    llm_server, sdk_source, tmp_path, mode, choice, monkeypatch,
):
    root, server = llm_server
    listeners = []
    plugin, actor, model, _, _, approvals, _ = bundle(
        sdk_source, root, "openjiuwen", choice, interactive=True,
        register_memory_tools=False,
        notify_callback=lambda *args: [notify(*args) for notify in listeners],
    )
    runtime, _, _, _, _, scope, _ = sdk_source
    unit, = runtime.api.add(
        f"联系 {RECALL_MARKER} 密码：{SYNTHETIC_PASSWORD}",
        scope,
        security=sdk_security(scope),
    )
    assert unit.system_metadata["privacy_level"] == "S3"
    owner = plugin.new_session(replace(actor, agent_id="swarm-main"))
    binding = SwarmSessionBinding(plugin, owner)
    backend = plugin.connect_memory_backend(
        api=runtime.api, provider="swarm-prefetch",
        scope_for=lambda _: scope, security_for=lambda _: sdk_security(scope),
        authorize_use=lambda *args: True,
        detector_version_for=lambda _: plugin.inspector.version,
        protected_source_levels_for=lambda _: frozenset({"S2", "S3"}),
    )
    reader = backend.reader
    name = "mcp_local_send"

    class RewriteMcpArguments(AgentRail):
        async def before_tool_call(self, ctx):
            if ctx.inputs.tool_name == name:
                ctx.inputs.tool_args = json.dumps({"body": "report " + SAMPLE})

    receipt = tmp_path / "receipt.json"
    script = tmp_path / "mcp_server.py"
    script.write_text(
        "import json\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('local')\n"
        "@server.tool()\ndef send(body: str) -> str:\n"
        f"    path = Path({str(receipt)!r})\n"
        "    path.write_text(json.dumps({'body': body}), encoding='utf8')\n"
        "    return 'api_key=result-secret-value'\n"
        "server.run(transport='stdio')\n",
        encoding="utf8",
    )

    def responder(body, _path):
        recent = []
        for message in reversed(body["messages"]):
            if message["role"] == "user":
                break
            recent.append(message["role"])
        tool_names = {tool["function"]["name"] for tool in body.get("tools") or []}
        if name not in tool_names or "tool" in recent:
            message, reason = {"role": "assistant", "content": "done"}, "stop"
        else:
            message, reason = {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "mcp-call", "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps({"body": "report"}),
                    },
                }],
            }, "tool_calls"
        response = {
            "id": "swarm-test", "object": "chat.completion", "created": 1,
            "model": body["model"],
        }
        if body.get("stream"):
            if "tool_calls" in message:
                message["tool_calls"][0]["index"] = 0
            response["object"] = "chat.completion.chunk"
            chunks = [
                {**response, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                {**response, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            ]
            return {
                "type": "text/event-stream",
                "chunks": [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
                + ["data: [DONE]\n\n"],
            }
        return {
            "type": "application/json",
            "chunks": [json.dumps({
                **response,
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
            })],
        }

    server["responder"] = responder

    async def turn():
        inputs = {"query": "联系", "conversation_id": owner.session_id}
        if mode == "invoke":
            return await binding.invoke(inputs)
        source = (
            binding.ainteract(inputs, request_id=uuid4().hex)
            if mode == "interaction" else binding.astream(inputs)
        )
        async with aclosing(source) as stream:
            return [result async for result in stream][0]

    async def approve(task):
        async with asyncio.timeout(20):
            while not task.done():
                for request in plugin.pending(owner):
                    selected = ApprovalChoice.SEND_REDACTED
                    if request["action"] == name:
                        assert not receipt.exists()
                        with pytest.raises(SecurityDenied, match="运行期间"):
                            await binding.reconcile_mcp([])
                        selected = choice
                    plugin.decide(
                        actor=owner, approval_id=request["approval_id"],
                        version=request["version"], choice=selected,
                    )
                await asyncio.sleep(0.01)
            return await task

    async def product_run(params):
        monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
        monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
        from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk, ReqMethod
        from jiuwenswarm.server.runtime.agent_adapter import interface
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        from openjiuwen.core.runner.callback.events import ToolCallEvents

        authenticated = replace(actor, session_id="work-code-test", agent_id="swarm-main")
        native_owner = replace(authenticated, session_id="native-after-switch")
        clients = {}

        async def authenticate(request, _mode):
            if request.user_id != authenticated.user_id:
                raise SecurityDenied("invalid owner")
            return replace(authenticated, session_id=request.session_id)

        async def open_session(_owner, _mode, notify):
            listeners.append(notify)
            return SwarmSessionPorts(
                plugin, model, reader, str(tmp_path), "Use the supplied tool.",
            )

        async def mcp_for(_owner, _mode, names):
            return [(name, "local-collector", clients[name]) for name in names]

        monkeypatch.setattr(interface, "create_adapter", interface.create_adapter)
        monkeypatch.setattr(swarm_control, "_HEARTBEAT_SECONDS", 0.05)
        store = swarm_control.SwarmProtectionStore(tmp_path / "control.sqlite3")
        assert store.public()["status"] == "unavailable"
        store.set_enabled(True, 0)
        close_control = await start_swarm_protection(
            authenticate=authenticate, open_session=open_session, mcp_for=mcp_for,
            state_path=tmp_path / "control.sqlite3",
        )
        assert store.public()["status"] == "enabled"
        facade = interface.JiuWenSwarm()
        base = JiuWenSwarmDeepAdapter if mode == "work" else JiuwenSwarmCodeAdapter

        async def native_reconcile(_self, *_args, **_kwargs):
            return None

        async def native_stream(_self, request, _inputs):
            yield AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": "native mode"},
                is_complete=True, agent_ref=request.agent_ref,
            )

        # Only the ordinary, unprotected native adapter is stubbed.
        monkeypatch.setattr(base, "reconcile_session_mcp", native_reconcile)
        monkeypatch.setattr(base, "process_message_stream_impl", native_stream)

        async def dispatch(params, identity=authenticated):
            request = AgentRequest(
                request_id=uuid4().hex, channel_id="web", session_id=identity.session_id,
                req_method=ReqMethod.CHAT_SEND, user_id=identity.user_id,
                params={"work_mode": mode, **params}, is_stream=True,
            )
            async with asyncio.timeout(20):
                return [item async for item in facade.process_message_stream(request)]

        async def complete(params):
            for _ in range(12):
                events = await dispatch(params)
                cards = [
                    item.payload for item in events
                    if (item.payload or {}).get("event_type") == "chat.ask_user_question"
                ]
                if not cards:
                    payloads = [item.payload for item in events if item.payload]
                    assert any(payload.get("content") == "done" for payload in payloads), payloads
                    return
                card, = cards
                public = next(
                    item for item in approvals if item["approval_id"] == card["request_id"]
                )
                selected = ApprovalChoice.SEND_REDACTED
                if public["action"] == name:
                    assert not receipt.exists()
                    assert {option["value"] for option in card["questions"][0]["options"]} == {
                        str(item) for item in ApprovalChoice
                    }
                    selected = choice
                params = {
                    "source": "ask_user_interrupt", "request_id": card["request_id"],
                    "answers": [{"selected_options": [str(selected)]}],
                }
            pytest.fail("Swarm chat exceeded the approval budget")

        # Exercise a host callback that rewrites the arguments before the send gate.
        async def rewrite(inputs, **kwargs):
            if inputs == {"body": "report"}:
                inputs = {"body": "report " + SAMPLE}
            return (inputs,), kwargs

        try:
            await Runner.callback_framework.register(
                ToolCallEvents.TOOL_INVOKE_INPUT, rewrite, callback_type="transform",
            )
            await complete({"query": "联系", "mcp": []})
            assert not receipt.exists()
            requested = store.set_enabled(False, 1)
            assert requested["status"] == "pending" and requested["effective"] is True
            async with asyncio.timeout(2):
                while store.public()["status"] == "pending":
                    await asyncio.sleep(0.01)
            assert store.public()["status"] == "disabled"
            events = await dispatch({"query": "new chat", "mcp": []}, native_owner)
            assert any(item.payload.get("content") == "native mode" for item in events)
            # The open, protected conversation still gets its normal approval.
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                clients["local"] = client
                await complete({"query": "联系", "mcp": ["local"]})
                clients.clear()
                await complete({"query": "联系", "mcp": []})
            assert await facade.cleanup_session_runtime(authenticated.session_id)
            assert not facade.has_session_runtime(authenticated.session_id)
        finally:
            await Runner.callback_framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, rewrite)
            try:
                await facade.cleanup()
            finally:
                await close_control()
            assert store.public()["status"] == "unavailable"

    async def run():
        await Runner.start()
        try:
            params = StdioServerParameters(command=sys.executable, args=[str(script)])
            if mode in {"work", "code"}:
                await product_run(params)
                return
            if mode != "invoke":
                agent = create_deep_agent(
                    model, card=AgentCard(id=owner.agent_id),
                    rails=[RewriteMcpArguments()],
                    workspace=str(tmp_path), system_prompt="Use the supplied tool.",
                    enable_sys_operation=False, enable_read_image_multimodal=False,
                    enable_model_anomaly_detection_rail=False, parallel_tool_calls=False,
                )
                binding.attach_agent(agent, reader=reader)
                if mode == "interaction":
                    from openjiuwen.harness.schema.interaction import SendInputRequest

                    session = create_agent_session(session_id=owner.session_id, card=agent.card)
                    await session.pre_run(inputs={})
                    await binding.start_interaction(session)
                    stream = await agent.attach_output()
                    async with aclosing(stream):
                        await agent.send_input(SendInputRequest(
                            request_id=uuid4().hex,
                            inputs={"query": "没有授权的轮次", "conversation_id": owner.session_id},
                        ))
                        frames = [frame async for frame in stream]
                    assert any(frame.type == "execution.error" for frame in frames)
                    assert not server["calls"]
            else:
                agent = binding.build_agent(
                    model=model, reader=reader,
                    workspace=str(tmp_path), system_prompt="Use the supplied tool.",
                )
                agent.add_rail(RewriteMcpArguments())
            with pytest.raises(SecurityDenied, match="会话身份"):
                await binding.invoke({"query": "联系", "conversation_id": "different-session"})
            assert await approve(asyncio.create_task(turn())) == {"content": "done"}
            assert not receipt.exists()
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                await binding.reconcile_mcp([("local", "local-collector", client)])
                original = agent.ability_manager.get(name)
                with pytest.raises(SecurityDenied, match="目标/schema"):
                    await binding.reconcile_mcp([("local", "current-user", client)])
                with pytest.raises(SecurityDenied, match="快照尚未完成"):
                    await turn()
                assert agent.ability_manager.get(name) == original
                await binding.reconcile_mcp([])
                assert agent.ability_manager.get(name) is None
                assert Runner.resource_mgr.get_tool(original.id) is None

            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                await binding.reconcile_mcp([("local", "local-collector", client)])
                assert agent.ability_manager.get(name).id == original.id
                assert await approve(asyncio.create_task(turn())) == {"content": "done"}
                await binding.aclose()
                assert agent.ability_manager.get(name) is None
                assert Runner.resource_mgr.get_tool(original.id) is None
                with pytest.raises(SecurityDenied, match="没有可运行"):
                    await turn()
        finally:
            await binding.aclose()
            await plugin.aclose()
            await Runner.stop()

    asyncio.run(run())
    if choice == ApprovalChoice.ABORT:
        assert not receipt.exists()
    else:
        expected = SAMPLE if choice == ApprovalChoice.CONTINUE_ORIGINAL else "[PRIVATE]"
        assert json.loads(receipt.read_text(encoding="utf8")) == {"body": "report " + expected}
    calls = json.dumps(server["calls"], ensure_ascii=False)
    assert "<memory-context>" in calls
    assert f"{RECALL_MARKER} 密码：[PRIVATE]" in calls
    assert SYNTHETIC_PASSWORD not in calls
    assert "jiuwen_memory_security_round" not in calls
    assert "13800138000" not in calls and "result-secret-value" not in calls and SAMPLE not in calls
    assert any(request["action"] == name for request in approvals)


def test_enabled_instance_denies_new_turn_if_worker_setup_fails(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter import interface

    from jiuwen_memory_adapter.jiuwenswarm.security_worker import guard_unavailable_worker

    async def check():
        path = tmp_path / "control.sqlite3"
        store = swarm_control.SwarmProtectionStore(path)
        store.set_enabled(True, 0)
        close = guard_unavailable_worker(path)
        try:
            for mode in ("work", "code"):
                native = interface.create_adapter("harness", mode=mode)
                with pytest.raises(SecurityDenied, match="保护未装配"):
                    await native.reconcile_session_mcp(
                        "new-session", [], execution_request=object(),
                    )
            assert store.public()["status"] == "unavailable"
        finally:
            await close()

    asyncio.run(check())


def test_swarm_nested_worker_callbacks_restore_outer_scope(llm_server, sdk_source, monkeypatch):
    root, _ = llm_server
    plugin, actor, *_ = bundle(
        sdk_source, root, "openjiuwen", ApprovalChoice.ABORT, register_memory_tools=False,
    )
    owner = plugin.new_session(actor)
    binding = SwarmSessionBinding(plugin, owner)
    run = AgentRunContext(owner, "worker-round")
    agent, session = object(), object()
    monkeypatch.setattr(binding, "_native", agent)
    monkeypatch.setattr(binding, "_session", session)
    monkeypatch.setattr(binding, "_active", run)
    monkeypatch.setattr(binding, "_round", "worker-round")
    rail = getattr(binding, "_interaction_rail")()
    inputs = SimpleNamespace(run_context=SimpleNamespace(
        extra={"jiuwen_memory_security_round": "worker-round"},
    ))
    outer = SimpleNamespace(agent=agent, session=session, inputs=inputs)
    inner = SimpleNamespace(agent=agent, session=session, inputs=inputs)

    async def check():
        try:
            await rail.before_invoke(outer)
            await rail.before_task_iteration(inner)
            await asyncio.create_task(rail.after_task_iteration(inner))
            await rail.after_invoke(outer)
            assert plugin.current_run() is run
            await rail.after_task_iteration(inner)
            assert plugin.current_run() is run
            await rail.after_invoke(outer)
            with pytest.raises(SecurityDenied):
                plugin.current_run()
            inputs.run_context.extra.clear()
            with pytest.raises(AbortError):
                await rail.before_task_iteration(inner)
        finally:
            await plugin.aclose()

    asyncio.run(check())
