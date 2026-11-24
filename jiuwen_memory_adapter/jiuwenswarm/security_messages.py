"""Bind native Swarm delegation to a single authenticated memory ledger."""

import asyncio
import inspect
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from uuid import uuid4
from weakref import WeakKeyDictionary

from ..agent_security._validation import has_exact_type
from ..agent_security.frameworks import AgentRunContext
from ..agent_security.hooks import Hook
from ..agent_security.runtime import Actor, SecurityDenied

MEMORY_PROVIDER = "swarm-memory"
_NON_TEXT_SUBAGENTS = {"browser_agent", "cua_agent", "mobile_gui_agent"}


@dataclass(frozen=True)
class _PendingSpawn:
    name: str
    actor: Actor
    ready: asyncio.Event


@dataclass
class _PendingTask:
    name: str
    parent: AgentRunContext
    text: str = field(repr=False)
    marker: str
    session_id: str | None = None
    dispatched: bool = False


class SwarmMessageGate:
    """Own the security identities for the original Agent and its native children."""

    def __init__(self, plugin, owner, *, child_id="swarm-subagent"):
        self._plugin = plugin
        self._owner = owner
        self._child_id = child_id
        self._children = {}
        self._bound_children = WeakKeyDictionary()
        self._pending_child = ContextVar("swarm_pending_child", default=None)

    def has_memory(self, actor):
        return any(
            source.provider == MEMORY_PROVIDER
            for source in self._plugin.sources.snapshot(actor).references
        )

    def child(self, native_session, *, fresh):
        """Never attach a persisted child history to a new security identity."""
        if not isinstance(native_session, str) or not native_session or len(native_session) > 256:
            raise SecurityDenied("子 Agent 会话标识无效")
        child = self._children.get(native_session)
        if child is not None:
            return child
        if fresh is not True:
            raise SecurityDenied("子 Agent 缺少当前 Worker 的记忆来源状态")
        child = replace(
            self._owner, agent_id=self._child_id, session_id=native_session,
            incarnation=uuid4().hex,
        )
        self._plugin.begin_delegate(child, self._owner)
        self._children[native_session] = child
        return child

    async def _start_child(self, native_session):
        from openjiuwen.core.session.checkpointer import CheckpointerFactory

        if (
            native_session not in self._children
            and await CheckpointerFactory.get_checkpointer().session_exists(native_session)
        ):
            raise SecurityDenied("子 Agent 历史缺少可信记忆来源")
        return self.child(native_session, fresh=True)

    async def send(self, source, recipient, text, *, deliver):
        """The actual queue/dispatch callable only receives the approved candidate."""
        if not isinstance(text, str):
            raise SecurityDenied("子 Agent 消息仅支持文本")
        parent_to_child = (
            source.actor == self._owner
            and self._children.get(recipient.session_id) == recipient
        )
        child_to_parent = (
            recipient == self._owner
            and self._children.get(source.actor.session_id) == source.actor
        )
        if not (parent_to_child or child_to_parent):
            raise SecurityDenied("子 Agent 消息未绑定到原会话")
        if not self.has_memory(source.actor):
            result = deliver(text)
            return await result if inspect.isawaitable(result) else result
        run = AgentRunContext(
            source.actor, source.turn, recipient, f"{source.turn}:message:{uuid4().hex}",
        )
        token = self._plugin.enter_run(run)
        try:
            return await self._plugin.delivery.asend(
                host=run, hook=Hook.BEFORE_AGENT_MESSAGE, payload={"body": text},
                send=lambda selected, _context: deliver(selected["body"]),
            )
        finally:
            self._plugin.leave_run(token)

    def bind_native(self, native, session, *, configure):
        """Bind children created by the native rail's registered delegation tools."""
        from openjiuwen.core.runner import Runner
        from openjiuwen.harness.rails.subagent.subagent_rail import SubagentRail
        from openjiuwen.harness.tools.subagent import SubagentSpawnTool, SubagentWaitTool, TaskTool
        from openjiuwen.harness.tools.subagent.subagent_tools import SubagentSendInputTool

        registered = []
        for rail in native.configured_rails():
            if not isinstance(rail, SubagentRail):
                continue
            for tool in rail.tools:
                card = native.ability_manager.get(tool.card.name)
                if card is None or Runner.resource_mgr.get_tool(card.id) is not tool:
                    continue
                if has_exact_type(tool, TaskTool) and tool.parent_agent is native:
                    self._bind_task_tool(tool)
                    registered.append(tool)
                elif has_exact_type(tool, (SubagentSpawnTool, SubagentSendInputTool,
                                           SubagentWaitTool)):
                    registered.append(tool)
        if registered:
            self._bind_factory(native, configure)
        if any(has_exact_type(tool, SubagentSpawnTool) for tool in registered):
            self._bind_persistent(native, session)
        return tuple(registered)

    def _bind_factory(self, native, configure):
        create = native.create_subagent

        def create_guarded(subagent_type, subsession_id, browser_capabilities=None):
            receipt = self._pending_child.get()
            if receipt is None:
                return create(subagent_type, subsession_id, browser_capabilities)
            if subagent_type != receipt.name:
                raise SecurityDenied("原生任务目标与消息收据不一致")
            if isinstance(receipt, _PendingSpawn):
                if subsession_id != receipt.actor.session_id:
                    raise SecurityDenied("子 Agent 创建会话与消息收据不一致")
                ready = receipt.ready
            else:
                if receipt.session_id is not None or browser_capabilities is not None:
                    raise SecurityDenied("原生任务目标与消息收据不一致")
                receipt.session_id = subsession_id
                ready = None
            child = create(subagent_type, subsession_id, browser_capabilities)
            self._bind_child_stream(child, subsession_id, configure, ready=ready)
            return child

        native.create_subagent = create_guarded

    def _bind_persistent(self, native, session):
        """Guard the native controller before it enqueues a child turn."""
        from openjiuwen.harness.kv_cache.kv_cache_subagent_lifecycle import (
            is_sticky_subagent_type,
        )
        from openjiuwen.harness.subagent_runtime.ids import build_subagent_id
        from openjiuwen.harness.subagent_runtime.models import SubagentStatusKind
        from openjiuwen.harness.tools.subagent import get_subagent_control

        control = get_subagent_control(native, session)
        spawn, send_input = control.spawn, control.send_input
        ready = {}

        async def spawn_guarded(subagent_type, query, *, subagent_id=None, **kwargs):
            parent = self._plugin.current_run()
            if self.has_memory(parent.actor):
                if subagent_type in _NON_TEXT_SUBAGENTS or kwargs.get("browser_capabilities"):
                    raise SecurityDenied("此子 Agent 不支持受保护文本交付")
                # Native status cards use these fields before the child's execution.
                kwargs.update(display_name=None, role=None)
            sid = subagent_id or build_subagent_id(
                session.get_session_id(), subagent_type,
                sticky=is_sticky_subagent_type(subagent_type),
            )
            if control.get_status(sid).kind not in (
                SubagentStatusKind.NOT_FOUND, SubagentStatusKind.CLOSED,
            ):
                raise SecurityDenied("子 Agent 已存在，请通过原生追发入口继续")
            release = ready.get(sid)
            if release is None:
                release = ready[sid] = asyncio.Event()
            elif not release.is_set():
                raise SecurityDenied("子 Agent 已有待交付消息")
            release.clear()
            try:
                child = await self._start_child(sid)

                async def deliver(text):
                    receipt = _PendingSpawn(subagent_type, child, release)
                    token = self._pending_child.set(receipt)
                    try:
                        return await spawn(subagent_type, text, subagent_id=sid, **kwargs)
                    finally:
                        self._pending_child.reset(token)

                return await self.send(parent, child, query, deliver=deliver)
            finally:
                release.set()

        async def send_guarded(subagent_id, query, **kwargs):
            parent = self._plugin.current_run()
            child = self.child(subagent_id, fresh=False)
            release = ready.get(subagent_id)
            if release is None or not release.is_set():
                raise SecurityDenied("子 Agent 缺少可交付的会话")
            release.clear()
            try:
                return await self.send(
                    parent, child, query,
                    deliver=lambda text: send_input(subagent_id, text, **kwargs),
                )
            finally:
                release.set()

        control.spawn = spawn_guarded
        control.send_input = send_guarded

    def _bind_task_tool(self, tool):
        native_invoke = tool.invoke

        async def invoke(inputs, **kwargs):
            parent = self._plugin.current_run()
            if not self.has_memory(parent.actor):
                return await native_invoke(inputs, **kwargs)
            if not has_exact_type(inputs, dict):
                raise SecurityDenied("原生子 Agent 工具缺少文本参数")
            name, text = inputs.get("subagent_type"), inputs.get("task_description")
            if not has_exact_type(name, str) or not has_exact_type(text, str) or not text:
                raise SecurityDenied("原生子 Agent 工具缺少文本参数")
            resume = str(inputs.get("resume_task_id") or "").strip()
            name = name.strip()
            if not name or resume or name in _NON_TEXT_SUBAGENTS:
                raise SecurityDenied("此子 Agent 的任务输入不支持受保护文本交付")
            receipt = _PendingTask(name, parent, text, f"MindVault task {uuid4().hex}")
            token = self._pending_child.set(receipt)
            try:
                result = await native_invoke(
                    {"subagent_type": name, "task_description": receipt.marker}, **kwargs,
                )
                if not receipt.dispatched:
                    raise SecurityDenied("原生任务缺少受保护消息收据")
                return result
            finally:
                self._pending_child.reset(token)

        tool.invoke = invoke

    def _bind_child_stream(self, native, session_id, configure, *, ready):
        """Check inputs and outputs at the native child's public text stream."""
        from openjiuwen.core.session.stream.base import OutputSchema
        from openjiuwen.harness.subagent_runtime.stream_output import TurnOutputAggregator

        identity = session_id, ready
        previous = self._bound_children.get(native)
        if previous is not None:
            if previous != identity:
                raise SecurityDenied("子 Agent 已绑定其他安全会话")
            return
        original = native.stream
        configured = False

        async def guarded(inputs, *, session=None, **options):
            nonlocal configured
            sid = session.get_session_id() if session is not None else inputs.get("conversation_id")
            if sid != session_id or inputs.get("conversation_id") != session_id:
                raise SecurityDenied("子 Agent 执行会话与消息收据不一致")
            if ready is not None:
                await ready.wait()
                actor = self.child(sid, fresh=False)
            else:
                receipt = self._pending_child.get()
                if not isinstance(receipt, _PendingTask) or receipt.session_id != sid:
                    raise SecurityDenied("原生任务缺少受保护消息收据")
                if receipt.dispatched or inputs.get("query") != receipt.marker:
                    raise SecurityDenied("原生任务参数在记忆交付前被改写")
                actor = await self._start_child(sid)
            if not configured:
                await configure(native, actor)
                configured = True
            if ready is None:
                receipt.dispatched = True
                selected = await self.send(
                    receipt.parent, actor, receipt.text, deliver=lambda approved: approved,
                )
                inputs = {**inputs, "query": selected}
            run = AgentRunContext(actor, uuid4().hex)
            token = self._plugin.enter_run(run)
            try:
                snapshot = self._plugin.sources.snapshot(actor)
                await asyncio.to_thread(self._plugin.sources.verify, actor, snapshot.version)
                if session is not None:
                    options["session"] = session
                stream = original(inputs, **options)
                async with aclosing(stream):
                    if not self.has_memory(actor):
                        async for frame in stream:
                            if self.has_memory(actor):
                                raise SecurityDenied("子 Agent 的记忆来源在流释放后改变")
                            yield frame
                        return

                    result = TurnOutputAggregator()
                    count = 0
                    async for frame in stream:
                        count += 1
                        if count > 4096:
                            raise SecurityDenied("子 Agent 流事件超过交付预算")
                        if getattr(frame, "type", None) not in {"llm_output", "answer"}:
                            continue
                        result.consume(frame)
                        if len(result.output().encode("utf8")) > 262144:
                            raise SecurityDenied("子 Agent 的结果超过交付预算")
                body = await self.send(run, self._owner, result.output(), deliver=lambda text: text)
                yield OutputSchema(
                    type="answer", index=0,
                    payload={
                        "output": body, "result_type": "error" if result.is_error() else "answer",
                    },
                )
            finally:
                self._plugin.leave_run(token)

        native.stream = guarded
        self._bound_children[native] = identity
