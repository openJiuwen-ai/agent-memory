"""受控原生 agent 的装配与调用；只把批准的最小响应交给应用。"""

import asyncio
from dataclasses import replace
from uuid import uuid4

from ._validation import has_exact_type
from .frameworks import AgentRunContext as _Run
from .hooks import Hook
from .runtime import SecurityDenied


class ManagedAgent:
    def __init__(self, plugin, native, agent_id):
        self._plugin, self._native, self.agent_id = plugin, native, agent_id
        self._delivery = plugin.delivery.create_binding()

    def belongs_to(self, plugin):
        return self._plugin is plugin

    def _begin(self, text, session, reply_to, checked_version=None):
        if (
            not has_exact_type(text, str)
            or len(text.encode()) > 262144
            or session.agent_id != self.agent_id
        ):
            raise SecurityDenied("受控 agent 的文本或会话不匹配")
        owner = self._plugin.owner_for(session)
        if reply_to is not None and self._plugin.owner_for(reply_to) != owner:
            raise SecurityDenied("agent 结果不能返回其他用户会话")
        state = self._plugin.sources.snapshot(session)
        if checked_version is None:
            self._plugin.sources.verify(session, state.version)
        elif state.version != checked_version:
            raise SecurityDenied("运行开始前来源已改变")
        run = _Run(session, uuid4().hex, reply_to)
        token = self._plugin.enter_run(run)
        return run, token

    async def _abegin(self, text, session, reply_to):
        state = self._plugin.sources.snapshot(session)
        await asyncio.to_thread(self._plugin.sources.verify, session, state.version)
        return self._begin(text, session, reply_to, state.version)

    def _inputs(self, text, session):
        return self._plugin.driver.inputs(text, session)

    async def ainvoke(self, text, *, session, reply_to=None):
        run, token = await self._abegin(text, session, reply_to)
        try:
            inputs, options = self._inputs(text, session)
            return await self._delivery.ainvoke(
                self._native,
                inputs,
                host=run,
                hook=Hook.BEFORE_AGENT_MESSAGE if reply_to else Hook.BEFORE_AGENT_RESPONSE,
                **options,
            )
        finally:
            self._plugin.leave_run(token)

    async def astream(self, text, *, session, reply_to=None):
        run, token = await self._abegin(text, session, reply_to)
        try:
            inputs, options = self._inputs(text, session)
            async for result in self._delivery.astream(
                self._native,
                inputs,
                host=run,
                hook=Hook.BEFORE_AGENT_MESSAGE if reply_to else Hook.BEFORE_AGENT_RESPONSE,
                **options,
            ):
                yield result
        finally:
            self._plugin.leave_run(token)


async def build_agent(
    plugin, *, agent_id, model, tools=(), middleware=(), rails=(), checkpointer=None
):
    tools = tuple(tools)
    host = plugin.runtime_context
    receipt = host.models.get(id(model))
    if plugin.closed or receipt is None or receipt[0] is not model:
        raise SecurityDenied("agent 含未登记的模型或工具")
    elif any(host.tools.get(id(tool)) is not tool for tool in tools):
        raise SecurityDenied("agent 含未登记的模型或工具")
    native = await plugin.driver.build_agent(
        host,
        agent_id=agent_id,
        model=model,
        tools=tools,
        middleware=middleware,
        rails=rails,
        checkpointer=checkpointer,
    )
    plugin.mark_bound()
    return ManagedAgent(plugin, native, agent_id)


async def delegate(plugin, child, body):
    parent = plugin.current_run()
    if not isinstance(child, ManagedAgent) or not child.belongs_to(plugin):
        raise SecurityDenied("接收 agent 不属于当前安全装配")
    actor = replace(
        parent.actor,
        agent_id=child.agent_id,
        session_id="jiuwen-" + uuid4().hex,
        incarnation=uuid4().hex,
    )
    plugin.begin_delegate(actor, parent.actor)
    transfer = replace(
        parent, recipient=actor, operation_id=f"{parent.turn}:delegate:{uuid4().hex}"
    )
    token = plugin.enter_run(transfer)
    packet = {}
    try:
        await plugin.delivery.asend(
            host=transfer,
            hook=Hook.BEFORE_AGENT_MESSAGE,
            payload={"body": body},
            send=lambda selected, context: packet.update(body=selected["body"]),
        )
    finally:
        plugin.leave_run(token)
    # Recipient mailbox now owns this frozen text. Parent lease has ended before child work/reply.
    if "body" not in packet:
        raise SecurityDenied("agent 消息未完成交付")
    result = await child.ainvoke(packet.get("body"), session=actor, reply_to=parent.actor)
    return await asyncio.to_thread(
        plugin.results.ensure,
        parent.actor,
        hook=Hook.BEFORE_AGENT_MESSAGE,
        producer=f"agent:{child.agent_id}",
        operation_id=f"{parent.turn}:received",
        value=result["content"],
    )
