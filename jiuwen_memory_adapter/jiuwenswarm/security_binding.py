"""显式装配 Swarm 会话中的原生自动召回和文本 MCP 工具。"""

import asyncio
import json
from collections.abc import Mapping
from contextlib import aclosing, asynccontextmanager, contextmanager
from contextvars import ContextVar
from uuid import uuid4

from ..agent_security._validation import has_exact_type
from ..agent_security.frameworks import AgentRunContext
from ..agent_security.hooks import Hook
from ..agent_security.memory import SdkMemoryReader
from ..agent_security.results import text_result
from ..agent_security.runtime import SecurityDenied, encode_payload
from .security_delivery import stream_native_rounds
from .security_messages import SwarmMessageGate
from .security_rail import SWARM_RAIL_CALLBACKS, JiuwenSecurityRail

_ROUND_KEY = "jiuwen_memory_security_round"


def _json_value(value):
    # 还原审批冻结的容器，保留获准的 JSON 值。
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class SwarmSessionBinding:
    """一份装配绑定一个已认证会话，由可信宿主在每轮调用时进入。"""

    def __init__(self, plugin, actor, *, recall_memory=True):
        if plugin.config.framework != "openjiuwen" or plugin.owner_for(actor) != actor:
            raise SecurityDenied("Swarm 接线需要 openjiuwen 和已登记的会话所有者")
        self._plugin, self._actor = plugin, actor
        self._recall_memory = recall_memory
        self._turn_lock = asyncio.Lock()
        self._tools = {}
        self._bindings = {}
        self._clients = {}
        self._installed = set()
        self._mcp_ready = True
        self._closed = False
        self._active = None
        self._native = None
        self._session = None
        self._product = None
        self._product_model = None
        self._product_tools = ()
        self._round = None
        self._worker = ContextVar("swarm_security_worker", default=None)
        self._rails = ()
        self._delivery = plugin.delivery.create_binding()

    def build_agent(self, *, model, reader, workspace, system_prompt, max_iterations=4):
        from openjiuwen.core.single_agent.schema.agent_card import AgentCard
        from openjiuwen.harness.factory import create_deep_agent

        if (
            self._closed or self._native is not None
            or id(model) not in self._plugin.runtime_context.models
        ):
            raise SecurityDenied("Swarm 会话已装配或模型未经安全装配")
        self._rails = (self._memory_rail(reader), *self._execution_rails())
        self._native = create_deep_agent(
            model,
            card=AgentCard(id=self._actor.agent_id),
            rails=list(self._rails),
            workspace=workspace,
            system_prompt=system_prompt,
            enable_sys_operation=False,
            enable_read_image_multimodal=False,
            enable_model_anomaly_detection_rail=False,
            parallel_tool_calls=False,
            max_iterations=max_iterations,
        )
        return self._native

    def attach_agent(self, agent, *, reader):
        """在原生宿主首次运行前装配同一个已配置 DeepAgent。"""
        from openjiuwen.harness.deep_agent import DeepAgent
        if self._closed or self._native is not None or not isinstance(agent, DeepAgent):
            raise SecurityDenied("Swarm 会话已装配或 agent 类型未覆盖")
        self._validate_agent(agent)
        rails = (self._memory_rail(reader), *self._execution_rails())
        for rail in rails:
            agent.add_rail(rail)
        self._rails, self._native = rails, agent
        return agent

    async def attach_product_agent(self, agent, session, *, reader, model):
        """Bind the original WorkSwarm agent and the interaction it already owns."""
        if self._native is not None or self._closed or agent.loop_session is not session:
            raise SecurityDenied("Swarm 原生会话无法装配")
        if id(model) not in self._plugin.runtime_context.models:
            raise SecurityDenied("Swarm 原生模型未通过安全装配")
        self._native, self._session = agent, session
        self._product_model = model
        self._product = SwarmMessageGate(self._plugin, self._actor)
        self._rails = (self._memory_rail(reader), *self._execution_rails())
        await self._scope_prompt_suggestions(agent)
        agent.deep_config.model = model
        agent.react_agent.set_llm(model)
        for rail in self._rails:
            await agent.register_rail(rail)
        self._product_tools = self._product.bind_native(
            agent, session, configure=self._configure_child,
        )
        self._validate_agent(agent)

    async def _scope_prompt_suggestions(self, agent):
        from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import (
            PromptSuggestionForkRail,
        )

        for rail in agent.configured_rails():
            if not has_exact_type(rail, PromptSuggestionForkRail):
                continue
            original = rail.after_model_call

            async def after_model_call(ctx, callback=original):
                if not self._product.has_memory(self._plugin.current_run().actor):
                    await callback(ctx)

            await agent.unregister_rail(rail)
            rail.after_model_call = after_model_call
            rail.priority = JiuwenSecurityRail.priority + 1
            await agent.register_rail(rail)

    async def _configure_child(self, child, actor):
        if not hasattr(child, "react_agent") or child.interaction_started:
            raise SecurityDenied("此子 Agent 暂不支持受保护执行")
        host = self._plugin.runtime_context
        child.deep_config.model = self._product_model
        child.react_agent.set_llm(self._product_model)
        await self._scope_prompt_suggestions(child)
        for rail in (
            self._plugin.llm.create_binding(),
            JiuwenSecurityRail(
                host.results,
                admit_model=lambda ctx: ctx.agent.get_llm() is self._product_model,
                admit_tool=lambda ctx: not self._product.has_memory(actor),
                current=self._plugin.current_run,
                inspect_results=lambda: self._product.has_memory(actor),
            ),
        ):
            await child.register_rail(rail)

    def _validate_agent(self, agent):
        from openjiuwen.harness.rails.memory.external_memory_rail import ExternalMemoryRail

        config = agent.deep_config
        installed = agent.configured_rails()
        guarded = set(SWARM_RAIL_CALLBACKS.values())
        invalid_config = "Swarm 原生配置包含未受控的执行或记忆路径"
        if config is None or agent.card.id != self._actor.agent_id:
            raise SecurityDenied(invalid_config)
        if id(config.model) not in self._plugin.runtime_context.models:
            raise SecurityDenied(invalid_config)
        if self._product is None and (config.mcps or config.subagents):
            raise SecurityDenied(invalid_config)
        if agent.interaction_started and self._session is None:
            raise SecurityDenied(invalid_config)
        if any(rail not in installed for rail in self._rails):
            raise SecurityDenied(invalid_config)
        for rail in installed:
            if rail in self._rails:
                continue
            if isinstance(rail, ExternalMemoryRail) and self._product is None:
                raise SecurityDenied(invalid_config)
            for event in guarded.intersection(rail.get_callbacks()):
                if rail.callback_priority(event) <= JiuwenSecurityRail.priority:
                    raise SecurityDenied(invalid_config)

    @contextmanager
    def _scope(self, run):
        token = self._plugin.enter_run(run)
        self._active = run
        try:
            yield run
        finally:
            self._active = None
            self._plugin.leave_run(token)

    @asynccontextmanager
    async def _turn(self, inputs, *, interaction=False):
        plugin, actor = self._plugin, self._actor
        if not has_exact_type(inputs, dict) or set(inputs) != {"query", "conversation_id"}:
            raise SecurityDenied("Swarm 文本或会话身份无效")
        query, conversation = inputs["query"], inputs["conversation_id"]
        if not has_exact_type(query, str) or not query.strip():
            raise SecurityDenied("Swarm 文本或会话身份无效")
        if not has_exact_type(conversation, str) or conversation != actor.session_id:
            raise SecurityDenied("Swarm 文本或会话身份无效")
        request = dict(inputs)
        async with self._turn_lock:
            if self._closed or self._native is None or plugin.closed:
                raise SecurityDenied("Swarm 会话没有可运行的装配")
            if interaction != (self._session is not None):
                raise SecurityDenied("Swarm 会话执行方式不一致")
            if not self._mcp_ready:
                raise SecurityDenied("Swarm MCP 快照尚未完成")
            self._validate_agent(self._native)
            state = plugin.sources.snapshot(actor)
            await asyncio.to_thread(plugin.sources.verify, actor, state.version)
            with self._scope(AgentRunContext(actor, uuid4().hex)) as run:
                yield run, request

    async def invoke(self, inputs):
        """调用本实例装配的原生 agent，交付使用 SDK 门禁。"""
        async with self._turn(inputs) as (run, request):
            result = await self._delivery.ainvoke(
                self._native, request, host=run, hook=Hook.BEFORE_AGENT_RESPONSE
            )
            self._current()
            return result

    async def astream(self, inputs):
        """宿主接收经门禁的最终输出，审批通过原生中断恢复。"""
        async with self._turn(inputs) as (run, request):
            stream = self._delivery.astream(
                self._native, request, host=run, hook=Hook.BEFORE_AGENT_RESPONSE
            )
            async with aclosing(stream):
                async for result in stream:
                    self._current()
                    yield result

    async def start_interaction(self, session):
        """Bind the host's native Session before starting its supervisor."""
        async with self._turn_lock:
            if self._closed or self._native is None or self._session is not None:
                raise SecurityDenied("Swarm 原生会话无法装配")
            matches = session.get_session_id() == self._actor.session_id
            if not matches or self._native.interaction_started:
                raise SecurityDenied("Swarm 原生会话无法装配")
            self._validate_agent(self._native)
            self._session = session
            try:
                await self._native.start(session=session)
            except BaseException:
                self._closed = True
                raise

    async def ainteract(self, inputs, *, request_id):
        """Dispatch through DeepAgent's native supervisor and scheduler."""
        from openjiuwen.harness.schema.interaction import SendInputRequest

        async with self._turn(inputs, interaction=True) as (run, request):
            async def round_stream(actual):
                stream = await self._native.attach_output()
                if stream is None:
                    raise SecurityDenied("Swarm 原生输出已被占用")
                marker = uuid4().hex
                self._round = marker
                try:
                    async with aclosing(stream):
                        await self._native.send_input(SendInputRequest(
                            request_id=request_id,
                            inputs={
                                **actual,
                                "conversation_id": self._session.get_session_id(),
                                "run": {"context": {_ROUND_KEY: marker}},
                            },
                        ))
                        async for frame in stream:
                            yield frame
                finally:
                    self._round = None

            stream = stream_native_rounds(
                self._plugin.delivery, self._native, request,
                host=run, round_stream=round_stream,
            )
            async with aclosing(stream):
                async for result in stream:
                    self._current()
                    yield result

    def _interaction_rail(self):
        from openjiuwen.core.runner.callback import AbortError
        from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, AgentRail

        scopes = ContextVar("swarm_security_worker_tokens", default=())

        def enter(ctx, event):
            if self._session is None:
                return
            native = ctx.inputs.run_context
            marker = native.extra.get(_ROUND_KEY) if native is not None else None
            same_session = ctx.agent is self._native and ctx.session is self._session
            same_round = isinstance(marker, str) and marker == self._round
            running = self._active is not None and not self._closed and not self._plugin.closed
            if not (same_session and same_round and running):
                raise AbortError("Swarm 后台轮次未登记", cause=SecurityDenied("轮次已拒绝"))
            run_token = self._plugin.enter_run(self._active)
            owner = (event, id(ctx), asyncio.current_task())
            scopes.set((*scopes.get(), (owner, run_token, self._worker.set(marker))))

        def leave(ctx, event):
            stack = scopes.get()
            owner = (event, id(ctx), asyncio.current_task())
            if stack and stack[-1][0] == owner:
                _, run_token, worker_token = stack[-1]
                scopes.set(stack[:-1])
                try:
                    self._worker.reset(worker_token)
                finally:
                    self._plugin.leave_run(run_token)

        class InteractionScope(AgentRail):
            priority = 2_000_000

            def callback_priority(self, event):
                if event in (
                    AgentCallbackEvent.AFTER_INVOKE,
                    AgentCallbackEvent.AFTER_TASK_ITERATION,
                ):
                    return -self.priority
                return self.priority

            async def before_invoke(self, ctx):
                enter(ctx, AgentCallbackEvent.BEFORE_INVOKE)

            async def after_invoke(self, ctx):
                leave(ctx, AgentCallbackEvent.BEFORE_INVOKE)

            async def before_task_iteration(self, ctx):
                enter(ctx, AgentCallbackEvent.BEFORE_TASK_ITERATION)

            async def after_task_iteration(self, ctx):
                leave(ctx, AgentCallbackEvent.BEFORE_TASK_ITERATION)

        return InteractionScope()

    def _current(self):
        run = self._plugin.current_run()
        worker = self._worker.get()
        stale_worker = worker is not None and worker != self._round
        if self._closed or run is not self._active or stale_worker:
            raise SecurityDenied("Swarm 会话与安全上下文不匹配")
        return run

    def _memory_rail(self, reader):
        from openjiuwen.core.memory.external.provider import MemoryProvider
        from openjiuwen.harness.rails.memory.external_memory_rail import ExternalMemoryRail

        if not isinstance(reader, SdkMemoryReader) or reader.sources is not self._plugin.sources:
            raise SecurityDenied("自动召回需要当前装配的 SDK 安全读取器")
        top_k = min(5, reader.max_items)
        current = self._current
        recall_memory = self._recall_memory

        class ReadOnlyProvider(MemoryProvider):
            name = "mindvault"

            @staticmethod
            def is_available():
                return True

            @staticmethod
            async def initialize(**kwargs):
                current()

            @staticmethod
            def get_tool_schemas():
                return []

            @staticmethod
            async def handle_tool_call(tool_name, args):
                raise SecurityDenied("自动召回没有独立工具入口")

            @staticmethod
            async def prefetch(query, **kwargs):
                run = current()
                if not recall_memory:
                    return ""
                views = await asyncio.to_thread(reader.search, query, run.actor, top_k=top_k)
                if current() is not run:
                    raise SecurityDenied("自动召回运行已改变")
                return encode_payload([
                    {"content": view.content, "abstract": view.abstract, "overview": view.overview}
                    for view in views
                ]) if views else ""

            @staticmethod
            async def sync_turn(user_msg, assistant_msg, **kwargs):
                # 本装配只取得 SDK 读取授权。
                return None

        return ExternalMemoryRail(
            ReadOnlyProvider(), user_id=self._actor.user_id, session_id=self._actor.session_id
        )

    def _execution_rails(self):
        from openjiuwen.core.runner import Runner

        host = self._plugin.runtime_context

        def admit_model(ctx):
            self._current()
            return id(ctx.agent.get_llm()) in host.models

        def admit_tool(ctx):
            run = self._current()
            name = ctx.inputs.tool_name
            if self._product is not None:
                if not self._product.has_memory(run.actor):
                    return True
                if self._native_message_tool(ctx):
                    return True
            tool = self._tools.get(name)
            card = ctx.agent.ability_manager.get(name)
            return (
                tool is not None and card is not None and card.id == tool.card.id
                and name in self._clients
                # A session lookup decorates the registered object for tracing.
                and Runner.resource_mgr.get_tool(tool.card.id) is tool
            )

        return [
            self._interaction_rail(),
            self._plugin.llm.create_binding(),
            self._plugin.execution.create_binding(),
            JiuwenSecurityRail(
                host.results, admit_tool=admit_tool, current=self._current, admit_model=admit_model,
                inspect_results=lambda: self._product is None
                or self._product.has_memory(self._current().actor),
            ),
        ]

    def _native_message_tool(self, ctx):
        from openjiuwen.core.runner import Runner

        card = ctx.agent.ability_manager.get(ctx.inputs.tool_name)
        if card is None:
            return False
        tool = Runner.resource_mgr.get_tool(card.id)
        return any(tool is registered for registered in self._product_tools)

    def _new_mcp_tool(self, name, binding):
        from mcp.types import CallToolResult, TextContent
        from openjiuwen.core.common.utils.schema_utils import SchemaUtils

        target, remote_name, description, schema_json = binding

        def prepare(arguments, _context):
            self._current()
            values = _json_value(arguments)
            schema = json.loads(schema_json)
            if schema is None:
                return values
            formatted = SchemaUtils.format_with_schema(values, schema, skip_none_value=False)
            return SchemaUtils.remove_none_values(formatted) or {}

        async def send(arguments, _context):
            self._current()
            client = self._clients.get(name)
            if client is None:
                raise SecurityDenied("MCP 工具已经卸载")
            result = await client.call_tool(remote_name, arguments=_json_value(arguments))
            if not isinstance(result, CallToolResult) or result.isError:
                raise SecurityDenied("MCP 调用未成功")
            if any(not isinstance(item, TextContent) for item in result.content):
                raise SecurityDenied("当前接线只接受文本 MCP 结果")
            return text_result({
                "content": [item.text for item in result.content],
                "structured": result.structuredContent,
            })

        tool = self._plugin.tool(
            target=target, name=name, description=description,
            input_schema=json.loads(schema_json), prepare_request=prepare, send=send,
        )
        if self._product is not None:
            from openjiuwen.core.foundation.tool import ToolExposure

            # Route selected MCP tools straight to their guarded callables.
            tool.card.exposure = ToolExposure.DIRECT
            tool.card.set_exposure_declared(True)
        tool.card.id = f"{name}_{uuid4().hex}"
        self._bindings[name] = binding
        self._tools[name] = tool
        return tool

    async def reconcile_mcp(self, servers):
        """从可信宿主选定的 (server_name, policy_target, ClientSession) 发现工具。"""
        if self._turn_lock.locked():
            raise SecurityDenied("受控轮次运行期间不可改写 MCP 工具")
        async with self._turn_lock:
            if self._closed or self._native is None or self._plugin.closed:
                raise SecurityDenied("Swarm 会话没有可更新的装配")
            self._validate_agent(self._native)
            self._mcp_ready = False
            self._clients.clear()
            desired = await self._discover_mcp(servers)
            for name in self._installed - desired.keys():
                self._unregister_mcp(name)
            self._register_mcp(desired)
            self._mcp_ready = True

    async def _discover_mcp(self, servers):
        from mcp import ClientSession
        from openjiuwen.core.foundation.tool.mcp.base import mcp_model_tool_name
        from openjiuwen.core.runner import Runner

        manager = self._native.ability_manager
        desired = {}
        seen = set()
        for server, target, client in servers:
            if not isinstance(client, ClientSession) or not server or server in seen:
                raise SecurityDenied("MCP 服务器名称重复或协议会话无效")
            seen.add(server)
            listing = await client.list_tools()
            if listing.nextCursor is not None:
                raise SecurityDenied("MCP 发现结果不完整")
            for item in listing.tools:
                name = mcp_model_tool_name(server, item.name)
                binding = (
                    target, item.name, item.description or "", encode_payload(item.inputSchema),
                )
                if name in desired or self._bindings.get(name, binding) != binding:
                    raise SecurityDenied("MCP 工具名称重复或目标/schema 已变化")
                existing = manager.get(name)
                tool = self._tools.get(name)
                if existing is not None:
                    if tool is None or existing.id != tool.card.id:
                        raise SecurityDenied("MCP 工具名称已由其他能力占用")
                    if Runner.resource_mgr.get_tool(existing.id) is not tool:
                        raise SecurityDenied("MCP 工具名称已由其他能力占用")
                desired[name] = (binding, client)
        return desired

    def _register_mcp(self, desired):
        from openjiuwen.core.runner import Runner

        if self._closed or self._plugin.closed:
            raise SecurityDenied("Swarm 会话已关闭")
        manager = self._native.ability_manager
        for name, (binding, _) in desired.items():
            tool = self._tools.get(name) or self._new_mcp_tool(name, binding)
            if Runner.resource_mgr.get_tool(tool.card.id) is None:
                Runner.resource_mgr.add_tool(tool)
            self._installed.add(name)
            if Runner.resource_mgr.get_tool(tool.card.id) is not tool:
                raise SecurityDenied("MCP 原生工具注册失败")
            if manager.get(name) is None:
                manager.add(tool.card)
            if manager.get(name) is None or manager.get(name).id != tool.card.id:
                raise SecurityDenied("MCP 原生能力注册失败")
        self._clients.update({name: client for name, (_, client) in desired.items()})

    def _unregister_mcp(self, name):
        from openjiuwen.core.runner import Runner

        manager = self._native.ability_manager
        tool = self._tools[name]
        existing = manager.get(name)
        if existing is not None and existing.id == tool.card.id:
            manager.remove(name)
        if Runner.resource_mgr.get_tool(tool.card.id) is tool:
            Runner.resource_mgr.remove_tool(tool.card.id)
        self._installed.remove(name)

    async def aclose(self):
        """撤销当前装配并释放注册项，MCP 连接仍归宿主所有。"""
        self._closed = True
        self._clients.clear()
        async with self._turn_lock:
            for name in tuple(self._installed):
                self._unregister_mcp(name)
            if self._session is not None:
                try:
                    await self._native.stop()
                finally:
                    self._session = None
