"""公开 Runner 边界；原生审批恢复，缓冲后只交付批准文本。"""

import asyncio

from .._validation import has_exact_type
from ..approval import approval_reply
from ..hooks import Hook
from ..runtime import SecurityDenied


def create_binding(adapter):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.core.session.stream.base import BaseStreamMode, OutputSchema

    def before(agent, inputs, host, hook, options):
        context = adapter.resolve_context(host, hook)
        state = adapter.sources.snapshot(context.actor)
        adapter.sources.verify(context.actor, state.version)
        if (
            set(options) - {"session"}
            or not isinstance(inputs, dict)
            or inputs.get("conversation_id") != context.actor.session_id
        ):
            raise SecurityDenied("原生 agent、会话或调用选项与安全上下文不一致")
        elif agent.card.id != context.actor.agent_id:
            raise SecurityDenied("原生 agent、会话或调用选项与安全上下文不一致")

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

    class OpenJiuwenDelivery:
        framework = "openjiuwen"

        async def ainvoke(self, agent, inputs, *, host, hook=Hook.BEFORE_AGENT_RESPONSE, **options):
            await asyncio.to_thread(before, agent, inputs, host, hook, options)
            for _ in range(128):
                result = await Runner.run_agent(agent=agent, inputs=inputs, **options)
                if result.get("result_type") != "interrupt":
                    return await deliver(result, host, hook)
                publics = [
                    item.payload.value.metadata["jiuwen_security"] for item in result["state"]
                ]
                inputs = await continuation(publics, inputs, host, hook)
            raise SecurityDenied("原生审批恢复预算耗尽")

        async def astream(self, agent, inputs, *, host, hook=Hook.BEFORE_AGENT_RESPONSE, **options):
            await asyncio.to_thread(before, agent, inputs, host, hook, options)
            count = 0
            for _ in range(128):
                final, publics = None, []
                async for frame in Runner.run_agent_streaming(
                    agent=agent, inputs=inputs, stream_modes=[BaseStreamMode.OUTPUT], **options
                ):
                    count += 1
                    if count > 4096:
                        raise SecurityDenied("agent 流事件预算耗尽")
                    if isinstance(frame, OutputSchema) and isinstance(frame.payload, dict):
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

    return OpenJiuwenDelivery()
