"""Swarm Work/Code composition for protected text turns."""

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass, field, replace

from ..agent_security.runtime import Actor, SecurityDenied
from .security_binding import SwarmSessionBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwarmSessionPorts:
    """Services assembled by the authenticated Swarm host for a single session."""

    plugin: object
    model: object
    reader: object
    workspace: str
    system_prompt: str
    recall_memory: bool = True


@dataclass
class _Dialogue:
    owner: Actor
    actor: Actor
    plugin: object
    binding: SwarmSessionBinding
    changed: asyncio.Event
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn: asyncio.Task | None = None

    def pending(self):
        return self.plugin.pending(self.actor)

    def answer(self, request):
        params = request.params
        answers = params.get("answers")
        public = next(
            (item for item in self.pending() if item["approval_id"] == params.get("request_id")),
            None,
        )
        if self.turn is None or public is None:
            raise SecurityDenied("Swarm 审批答复与待处理请求不一致")
        if not isinstance(answers, list) or len(answers) != 1:
            raise SecurityDenied("Swarm 审批答复与待处理请求不一致")
        answer = answers[0]
        if not isinstance(answer, dict) or answer.get("custom_input"):
            raise SecurityDenied("Swarm 审批答复与待处理请求不一致")
        choices = answer.get("selected_options")
        if not isinstance(choices, list) or len(choices) != 1 or choices[0] not in {
            option["value"] for option in public["options"]
        }:
            raise SecurityDenied("Swarm 审批必须选择一个已登记的选项")
        self.plugin.decide(
            actor=self.actor, approval_id=public["approval_id"], version=public["version"],
            choice=choices[0],
        )

    async def next_event(self):
        while True:
            self.changed.clear()
            if self.turn is None:
                raise SecurityDenied("Swarm 受保护轮次已取消")
            if self.turn.done():
                task, self.turn = self.turn, None
                return {"event_type": "chat.final", "content": task.result()["content"]}, False
            pending = self.pending()
            if pending:
                return _approval_event(pending[0]), True
            await self.changed.wait()

    async def aclose(self):
        await self.cancel_turn()
        try:
            await self.binding.aclose()
        finally:
            await self.plugin.aclose()

    async def cancel_turn(self):
        if self.turn is None:
            return 0
        task, self.turn = self.turn, None
        cancelled = task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return int(cancelled)


def _approval_event(public):
    return {
        "event_type": "chat.ask_user_question",
        "request_id": public["approval_id"],
        "source": "ask_user_interrupt",
        "questions": [{
            "header": "敏感信息外发审批",
            "question": f"是否允许本次操作向目标 {public['target']} 发送内容？",
            "options": public["options"],
            "multi_select": False,
        }],
    }


def _is_answer(request):
    return (
        request.params.get("source") == "ask_user_interrupt"
        and isinstance(request.params.get("answers"), list)
        and bool(request.params["answers"])
    )


class _Dialogues:
    def __init__(self, mode, authenticate, open_session, mcp_for, native_session=None):
        self._mode = mode
        self._authenticate = authenticate
        self._open_session = open_session
        self._mcp_for = mcp_for
        self._native_session = native_session
        self._sessions = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def authenticate(self, request):
        actor = await self._authenticate(request, self._mode)
        if not isinstance(actor, Actor) or actor.session_id != request.session_id:
            raise SecurityDenied("缺少可信 Swarm 会话身份")
        return actor

    async def _session(self, owner, request):
        from openjiuwen.core.session.agent import create_agent_session

        async with self._lock:
            if self._closed:
                raise SecurityDenied("Swarm 宿主已关闭")
            state = self._sessions.get(owner.session_id)
            if state is not None:
                if state.owner != owner:
                    raise SecurityDenied("Swarm 会话所有者不匹配")
                return state
            if len(self._sessions) >= 128:
                raise SecurityDenied("Swarm 受保护会话容量已满")
            loop = asyncio.get_running_loop()
            changed = asyncio.Event()

            def notify(_actor, _event):
                loop.call_soon_threadsafe(changed.set)

            product = await self._native_session(request) if self._native_session else None
            identity = replace(owner, agent_id=product[0].card.id) if product else owner
            ports = await self._open_session(identity, self._mode, notify)
            plugin = ports.plugin
            binding = None
            try:
                actor = plugin.new_session(identity)
                binding = SwarmSessionBinding(plugin, actor, recall_memory=ports.recall_memory)
                if product:
                    await binding.attach_product_agent(
                        *product, reader=ports.reader, model=ports.model,
                    )
                else:
                    agent = binding.build_agent(
                        model=ports.model, reader=ports.reader, workspace=ports.workspace,
                        system_prompt=ports.system_prompt,
                    )
                    session = create_agent_session(session_id=actor.session_id, card=agent.card)
                    await session.pre_run(inputs={})
                    await binding.start_interaction(session)
            except BaseException:
                if binding is not None:
                    await binding.aclose()
                await plugin.aclose()
                raise
            state = _Dialogue(owner, actor, plugin, binding, changed)
            self._sessions[owner.session_id] = state
            return state

    async def reconcile(self, request, session_id, needed):
        if request is None or session_id != request.session_id:
            raise SecurityDenied("Swarm MCP 更新缺少已认证请求")
        owner = await self.authenticate(request)
        if not _is_answer(request):
            # Discovery belongs to the same serialized turn as dispatch.
            return owner, tuple(needed or ())
        return owner, ()

    async def stream(self, request, inputs, selection):
        from jiuwenswarm.common.schema.agent import AgentResponseChunk, ReqMethod

        def chunk(payload, *, complete=False, outcome=None):
            return AgentResponseChunk(
                request_id=request.request_id, channel_id=request.channel_id,
                payload=payload, is_complete=complete, agent_ref=request.agent_ref,
                metadata={key: value for key, value in (request.metadata or {}).items()
                          if key != "auth_session"},
                runtime_completion=outcome,
            )

        try:
            runtime = request.trusted_runtime_binding
            if request.req_method != ReqMethod.CHAT_SEND or (
                runtime is not None and runtime.get("kind") != "deep_agent"
            ):
                raise SecurityDenied("受保护 Work / Code 只接受本地文本对话")
            owner = await self.authenticate(request)
            if selection is not None and selection[0] != owner:
                raise SecurityDenied("Swarm MCP 选择与请求主体不一致")
            state = await self._session(owner, request)
            async with state.request_lock:
                if _is_answer(request):
                    state.answer(request)
                else:
                    if state.turn is not None and state.turn.done():
                        await state.cancel_turn()
                    if state.turn is not None or selection is None:
                        raise SecurityDenied("Swarm 对话已有待处理轮次或缺少 MCP 选择")
                    selected = await self._mcp_for(owner, self._mode, selection[1])
                    await state.binding.reconcile_mcp(selected)

                    async def run():
                        stream = state.binding.ainteract(
                            {"query": inputs["query"], "conversation_id": state.actor.session_id},
                            request_id=request.request_id,
                        )
                        async with aclosing(stream):
                            return await anext(stream)

                    state.turn = asyncio.create_task(run())
                    state.turn.add_done_callback(lambda _: state.changed.set())
                try:
                    event, suspended = await state.next_event()
                except asyncio.CancelledError:
                    await state.cancel_turn()
                    raise
            yield chunk(event)
            yield chunk({"event_type": "chat.done"}, complete=True,
                        outcome="suspended" if suspended else None)
        except SecurityDenied:
            yield chunk(
                {"event_type": "chat.error", "error": "安全策略拒绝了本次操作"},
                complete=True,
            )
        except Exception as exc:
            logger.warning("Swarm protected turn failed (%s)", type(exc).__name__)
            yield chunk({"event_type": "chat.error", "error": "受保护会话执行失败"}, complete=True)

    def has_runtime(self, session_id):
        return bool(self._sessions) if session_id is None else session_id in self._sessions

    async def cancel(self, session_id):
        state = self._sessions.get(session_id)
        return await state.cancel_turn() if state is not None else 0

    async def evict(self, session_id):
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.request_lock.locked():
                return False
            if state.turn is not None and not state.turn.done():
                return False
            del self._sessions[session_id]
        await state.aclose()
        return True

    async def aclose(self):
        async with self._lock:
            self._closed = True
            sessions, self._sessions = tuple(self._sessions.values()), {}
        await asyncio.gather(*(state.aclose() for state in sessions))


def install_swarm_work_code(
    *, authenticate, open_session, mcp_for, control=None, native_product=False,
):
    """Install the protected Work/Code composition at Swarm's facade factory."""
    from jiuwenswarm.server.runtime.agent_adapter import interface
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import resolve_sdk_choice

    create_adapter = interface.create_adapter
    if getattr(create_adapter, "_mindvault_installed", False):
        raise RuntimeError("Swarm Work / Code 已装配")

    def factory(sdk=None, *, mode="agent"):
        if mode == "work":
            mode = "agent"
        if mode not in {"agent", "code"}:
            return create_adapter(sdk, mode=mode)
        if (sdk or resolve_sdk_choice()) != "harness":
            if control is not None and control.enabled:
                raise SecurityDenied("已启用保护的 Swarm 会话须使用 harness")
            return create_adapter(sdk, mode=mode)
        if mode == "code":
            from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
                JiuwenSwarmCodeAdapter,
            )

            native = JiuwenSwarmCodeAdapter
        else:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                JiuWenSwarmDeepAdapter,
            )

            native = JiuWenSwarmDeepAdapter

        class WorkCode(native):
            def __init__(self):
                super().__init__()
                self._protected = _Dialogues(
                    mode, authenticate, open_session, mcp_for,
                    self._original_session if native_product else None,
                )
                self._session_modes = {}

            async def _original_session(self, request):
                child = await self._get_session_adapter_for_request(request, reserve_activity=False)
                agent = child.get_live_session_instance(request.session_id)
                session = agent.loop_session if agent is not None else None
                if session is None or session.get_session_id() != request.session_id:
                    raise SecurityDenied("Swarm 未提供当前会话的原生 DeepAgent")
                context = agent.react_agent.context_engine.get_context(
                    session_id=session.get_session_id(),
                )
                if context is not None and context.get_messages():
                    raise SecurityDenied("需要新会话才能登记记忆来源")
                return agent, session

            async def reconcile_session_mcp(
                self, session_id, needed, *, execution_request=None, **options,
            ):
                if execution_request is None:
                    return
                enabled = True
                if control is not None:
                    current = control.enabled
                    # Existing conversations keep their original security context across changes.
                    enabled = self._session_modes.get(session_id, current)
                    if session_id not in self._session_modes and len(self._session_modes) >= 256:
                        raise SecurityDenied("Swarm 已达到会话装配容量")
                if not enabled:
                    await super().reconcile_session_mcp(
                        session_id, needed, execution_request=execution_request, **options,
                    )
                    if control is not None and session_id:
                        self._session_modes[session_id] = False
                    vars(execution_request)["_mindvault_native"] = self
                    return
                if interface.get_memory_mode(interface.get_config()) == "cloud":
                    raise SecurityDenied("受保护会话需要关闭 Swarm 云记忆旁路")
                selection = await self._protected.reconcile(execution_request, session_id, needed)
                if control is not None:
                    self._session_modes[session_id] = True
                vars(execution_request)["_mindvault_mcp_selection"] = (self, selection)

            async def process_message_stream_impl(self, request, inputs):
                if vars(request).pop("_mindvault_native", None) is self:
                    native_stream = super().process_message_stream_impl(request, inputs)
                    async with aclosing(native_stream) as stream:
                        async for chunk in stream:
                            yield chunk
                    return
                selected_by, selection = vars(request).pop("_mindvault_mcp_selection", (None, None))
                if selected_by is not self:
                    selection = None
                async with aclosing(self._protected.stream(request, inputs, selection)) as stream:
                    async for chunk in stream:
                        yield chunk

            async def process_message_impl(self, request, inputs):
                from jiuwenswarm.common.schema.agent import AgentResponse

                if vars(request).pop("_mindvault_native", None) is self:
                    return await super().process_message_impl(request, inputs)
                payload = None
                async for chunk in self.process_message_stream_impl(request, inputs):
                    if chunk.payload["event_type"] != "chat.done":
                        payload = chunk.payload
                return AgentResponse(
                    request.request_id, request.channel_id,
                    ok=payload["event_type"] != "chat.error", payload=payload,
                    metadata={key: value for key, value in (request.metadata or {}).items()
                              if key != "auth_session"},
                    agent_ref=request.agent_ref,
                )

            async def cleanup(self):
                try:
                    await self._protected.aclose()
                    self._session_modes.clear()
                finally:
                    await super().cleanup()

            def has_session_runtime(self, session_id=None):
                return (
                    self._protected.has_runtime(session_id)
                    or super().has_session_runtime(session_id)
                )

            async def cleanup_session_adapter(self, session_id):
                protected_removed = await self._protected.evict(session_id)
                native_removed = await super().cleanup_session_adapter(session_id)
                removed = protected_removed or native_removed
                if removed:
                    self._session_modes.pop(session_id, None)
                return removed

            async def cancel_session_tasks(self, session_id):
                cancelled = await self._protected.cancel(session_id)
                return cancelled + await super().cancel_session_tasks(session_id)

        return WorkCode()

    vars(factory)["_mindvault_installed"] = True
    interface.create_adapter = factory


    def uninstall():
        if interface.create_adapter is factory:
            interface.create_adapter = create_adapter

    return uninstall


async def start_swarm_protection(
    *, authenticate, open_session, mcp_for, state_path=None, native_product=False,
    configure_policy=False,
):
    """Mount the switch in the resident AgentServer Worker before it creates adapters."""
    from jiuwenswarm.enterprise.edition import is_enterprise
    from jiuwenswarm.server.runtime.agent_adapter import interface
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import resolve_sdk_choice

    from .security_control import SwarmProtectionRuntime, SwarmProtectionStore
    from .security_install import swarm_infra_dir

    path = state_path or swarm_infra_dir() / "mindvault" / "control.sqlite3"
    runtime = SwarmProtectionRuntime(await asyncio.to_thread(SwarmProtectionStore, path))

    async def configured_session(actor, mode, notify):
        return await open_session(actor, mode, notify, policy=runtime.policy)

    uninstall = install_swarm_work_code(
        authenticate=authenticate, mcp_for=mcp_for, control=runtime,
        open_session=configured_session if configure_policy else open_session,
        native_product=native_product,
    )

    def ready():
        return (
            not is_enterprise() and resolve_sdk_choice() == "harness"
            and interface.get_memory_mode(interface.get_config()) != "cloud"
        )

    try:
        await runtime.start(ready)
    except BaseException:
        uninstall()
        await runtime.aclose()
        raise

    async def close():
        await runtime.aclose()
        uninstall()

    return close
