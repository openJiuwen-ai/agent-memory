"""Map MindVault model and tool boundaries onto the Swarm DeepAgent Rail."""

import asyncio
from types import MappingProxyType

from openjiuwen.core.foundation.tool.schema import ToolOutput, ToolTimeoutResult
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, AgentRail

from ..agent_security.hooks import Hook
from ..agent_security.runtime import SecurityDenied

SWARM_RAIL_CALLBACKS = MappingProxyType({
    Hook.BEFORE_LLM_CALL: AgentCallbackEvent.BEFORE_MODEL_CALL,
    Hook.AFTER_LLM_CALL: AgentCallbackEvent.AFTER_MODEL_CALL,
    Hook.BEFORE_TOOL_CALL: AgentCallbackEvent.BEFORE_TOOL_CALL,
    Hook.AFTER_TOOL_CALL: AgentCallbackEvent.AFTER_TOOL_CALL,
})


class JiuwenSecurityRail(AgentRail):
    """Protect a DeepAgent with host-owned model, tool and source receipts."""

    priority = -1_000_000

    def __init__(self, results, *, admit_model, admit_tool, current, inspect_results=None):
        self._results = results
        self._admit_model = admit_model
        self._admit_tool = admit_tool
        self._current = current
        self._inspect_results = inspect_results or (lambda: True)

    def get_callbacks(self):
        return {event: getattr(self, event.value) for event in SWARM_RAIL_CALLBACKS.values()}

    @staticmethod
    def _require_admitted(check, ctx, target):
        try:
            admitted = check(ctx) is True
        except Exception:
            admitted = False
        if not admitted:
            raise AbortError(f"实际{target}未登记", cause=SecurityDenied(f"{target}已拒绝"))

    async def before_model_call(self, ctx):
        self._require_admitted(self._admit_model, ctx, "模型")

    async def before_tool_call(self, ctx):
        self._require_admitted(self._admit_tool, ctx, "工具")

    async def _message(self, message, hook, producer):
        if message is None:
            return
        run = self._current()
        body = message.model_dump(mode="json")
        checked = await asyncio.to_thread(
            self._results.ensure,
            run.actor,
            hook=hook,
            producer=producer,
            operation_id=f"{run.turn}:projection",
            value=body,
        )
        for field in ("role", "tool_call_id", "name"):
            if body.get(field) != checked.get(field):
                raise SecurityDenied("结果安全视图改变消息身份")
        if [(tool.get("id"), tool.get("name")) for tool in body.get("tool_calls") or []] != [
            (tool.get("id"), tool.get("name")) for tool in checked.get("tool_calls") or []
        ]:
            raise SecurityDenied("结果安全视图改变工具身份")
        safe = type(message).model_validate(checked)
        # Native @rail returns the same message. Apply its checked projection in place.
        for field in type(message).model_fields:
            setattr(message, field, getattr(safe, field))

    async def after_model_call(self, ctx):
        if not self._inspect_results():
            return
        try:
            await self._message(
                ctx.inputs.response, Hook.AFTER_LLM_CALL, "framework-model-result",
            )
        except Exception:
            raise AbortError("模型结果未释放", cause=SecurityDenied("结果检查未通过")) from None

    async def after_tool_call(self, ctx):
        if not self._inspect_results():
            return
        try:
            await self._message(ctx.inputs.tool_msg, Hook.AFTER_TOOL_CALL, ctx.inputs.tool_name)
            if ctx.inputs.tool_result is not None:
                run = self._current()
                original = ctx.inputs.tool_result
                result_type = type(original)
                typed = result_type in (ToolOutput, ToolTimeoutResult)
                safe = await asyncio.to_thread(
                    self._results.ensure,
                    run.actor,
                    hook=Hook.AFTER_TOOL_CALL,
                    producer=ctx.inputs.tool_name,
                    operation_id=f"{run.turn}:tool-projection",
                    value=original.model_dump(mode="json") if typed else original,
                )
                ctx.inputs.tool_result = result_type.model_validate(safe) if typed else safe
        except Exception:
            raise AbortError("工具结果未释放", cause=SecurityDenied("结果检查未通过")) from None
