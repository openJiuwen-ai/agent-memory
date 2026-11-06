"""openjiuwen 的原生装配驱动；第三方导入只发生在显式创建时。"""

import asyncio

from .._validation import has_exact_type
from ..hooks import Hook
from ..runtime import SecurityDenied
from ._native_tools import create_openjiuwen_tool


class OpenJiuwenRuntimeDriver:
    framework = "openjiuwen"
    create_tool = staticmethod(create_openjiuwen_tool)

    @staticmethod
    def create_execution(guard, **ports):
        from ._execution_openjiuwen import OpenJiuwenExecutionAdapter

        return OpenJiuwenExecutionAdapter(guard, **ports)

    @staticmethod
    def create_llm_binding(adapter):
        from ._llm_openjiuwen import create_binding

        return create_binding(adapter)

    @staticmethod
    def create_delivery_binding(adapter):
        from ._delivery_openjiuwen import create_binding

        return create_binding(adapter)

    @staticmethod
    def llm_task(request):
        return "turn"

    @staticmethod
    def inputs(text, session):
        return {"query": text, "conversation_id": session.session_id}, {}

    @staticmethod
    def register_llm_provider(adapter):
        from ._llm_openjiuwen import register_provider

        return register_provider(adapter)

    @staticmethod
    def create_model(host, target, *, api_key, streaming, max_retries, **options):
        base = target.endpoint.removesuffix("/chat/completions")
        from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

        provider = host.llm.register_native_provider()
        request = {k: v for k, v in options.items() if k != "timeout"}
        model = Model(
            model_client_config=ModelClientConfig(
                client_provider=provider,
                api_key=api_key,
                api_base=base,
                max_retries=max_retries,
                timeout=options.get("timeout", 60),
            ),
            model_config=ModelRequestConfig(model=target.model, **request),
        )
        return model

    async def build_agent(self, host, *, agent_id, model, tools, middleware, rails, checkpointer):
        if middleware or checkpointer is not None:
            raise SecurityDenied("九问只使用原生 Rail 和会话持久机制")
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.runner.callback import AbortError
        from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
        from openjiuwen.core.single_agent.rail.base import AgentRail
        from openjiuwen.core.single_agent.schema.agent_card import AgentCard

        class ProtectedReAct(ReActAgent):
            def set_llm(self, selected):
                if id(selected) not in host.models:
                    raise SecurityDenied("动态选模必须使用受保护候选")
                return super().set_llm(selected)

        owned = {tool.card.name: tool for tool in tools}
        if any(
            not has_exact_type(rail.priority, int) or rail.priority <= -1000000 for rail in rails
        ):
            raise SecurityDenied("宿主 Rail 必须在最终消费检查之前执行")

        class Attestation(AgentRail):
            priority = -1000000

            async def before_tool_call(self, ctx):
                tool = owned.get(ctx.inputs.tool_name)
                if tool is None or Runner.resource_mgr.get_tool(tool.card.id) is not tool:
                    raise AbortError("实际工具未登记", cause=SecurityDenied("工具已拒绝"))

            async def _message(self, message, hook, producer):
                if message is None:
                    return
                current = host.current()
                body = message.model_dump(mode="json")
                checked = await asyncio.to_thread(
                    host.results.ensure,
                    current.actor,
                    hook=hook,
                    producer=producer,
                    operation_id=f"{current.turn}:projection",
                    value=body,
                )
                for field in ("role", "tool_call_id", "name"):
                    if body.get(field) != checked.get(field):
                        raise SecurityDenied("结果安全视图改变消息身份")
                if [(t.get("id"), t.get("name")) for t in body.get("tool_calls") or []] != [
                    (t.get("id"), t.get("name")) for t in checked.get("tool_calls") or []
                ]:
                    raise SecurityDenied("结果安全视图改变工具身份")
                safe = type(message).model_validate(checked)
                # Native @rail returns the original object; mutate that same public message
                # after all host result Rails, before it reaches model context/history.
                for field in type(message).model_fields:
                    setattr(message, field, getattr(safe, field))

            async def after_model_call(self, ctx):
                try:
                    await self._message(
                        ctx.inputs.response, Hook.AFTER_LLM_CALL, "framework-model-result"
                    )
                except Exception:
                    raise AbortError(
                        "模型结果未释放", cause=SecurityDenied("结果检查未通过")
                    ) from None

            async def after_tool_call(self, ctx):
                try:
                    await self._message(
                        ctx.inputs.tool_msg, Hook.AFTER_TOOL_CALL, ctx.inputs.tool_name
                    )
                    if ctx.inputs.tool_result is not None:
                        current = host.current()
                        ctx.inputs.tool_result = await asyncio.to_thread(
                            host.results.ensure,
                            current.actor,
                            hook=Hook.AFTER_TOOL_CALL,
                            producer=ctx.inputs.tool_name,
                            operation_id=f"{current.turn}:tool-projection",
                            value=ctx.inputs.tool_result,
                        )
                except Exception:
                    raise AbortError(
                        "工具结果未释放", cause=SecurityDenied("结果检查未通过")
                    ) from None

        native = ProtectedReAct(card=AgentCard(id=agent_id))
        configuration = ReActAgentConfig()
        configuration.configure_prompt_template(
            [{"role": "system", "content": "Use the registered tools when needed."}]
        )
        native.configure(configuration)
        native.set_llm(model)
        for tool in tools:
            Runner.resource_mgr.add_tool(tool)
            native.ability_manager.add(tool.card)
        for rail in [
            host.llm.create_binding(),
            host.execution.create_binding(),
            *rails,
            Attestation(),
        ]:
            await native.register_rail(rail)
        return native
