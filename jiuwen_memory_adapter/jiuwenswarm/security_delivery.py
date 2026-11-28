"""Resume protected delivery through Swarm's native background supervisor."""

import asyncio
from contextlib import aclosing

from ..agent_security._validation import has_exact_type
from ..agent_security.approval import approval_reply
from ..agent_security.hooks import Hook
from ..agent_security.runtime import SecurityDenied


async def stream_native_rounds(adapter, agent, inputs, *, host, round_stream):
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.core.session.stream.base import OutputSchema

    hook = Hook.BEFORE_AGENT_RESPONSE

    def before():
        context = adapter.resolve_context(host, hook)
        state = adapter.sources.snapshot(context.actor)
        adapter.sources.verify(context.actor, state.version)
        if (
            not isinstance(inputs, dict)
            or inputs.get("conversation_id") != context.actor.session_id
            or agent.card.id != context.actor.agent_id
        ):
            raise SecurityDenied("Swarm 原生 agent 或会话与安全上下文不一致")

    async def continuation(publics, inputs, host, hook):
        if not publics or len(publics) > 128:
            raise SecurityDenied("原生中断数量无效")
        reply = InteractiveInput()
        for public in publics:
            choice = await adapter.aapprove_native(public, host=host, hook=hook)
            reply.update(public["operation_id"], approval_reply(public, choice))
        return {**inputs, "query": reply}

    async def deliver(result, host, hook):
        if (
            not result
            or result.get("result_type") != "answer"
            or not has_exact_type(result.get("output"), str)
        ):
            raise SecurityDenied("agent 未完成最终响应")
        return await adapter.asend(
            host=host,
            hook=hook,
            payload={"body": result["output"]},
            send=lambda selected, context: {"content": selected["body"]},
        )

    await asyncio.to_thread(before)
    count = 0
    for _ in range(128):
        final, publics = None, []
        async with aclosing(round_stream(inputs)) as stream:
            async for frame in stream:
                count += 1
                if count > 4096:
                    raise SecurityDenied("agent 流事件预算耗尽")
                if isinstance(frame, OutputSchema) and isinstance(frame.payload, dict):
                    if frame.type == "execution.error":
                        raise SecurityDenied("原生 agent 执行失败")
                    if "output" in frame.payload:
                        final = frame.payload
                payload = getattr(frame, "payload", None)
                metadata = getattr(getattr(payload, "value", None), "metadata", {})
                if metadata.get("jiuwen_security"):
                    publics.append(metadata["jiuwen_security"])
        if publics:
            inputs = await continuation(publics, inputs, host, hook)
        else:
            yield await deliver(final, host, hook)
            return
    raise SecurityDenied("原生审批恢复预算耗尽")
