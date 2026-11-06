"""公开 Rail 捕获身份作用域；合作 client 工厂通过公开注册表安装。"""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

from ..llm import chat_endpoint
from ..runtime import SecurityDenied


def create_binding(adapter):
    from openjiuwen.core.runner.callback import AbortError
    from openjiuwen.core.single_agent.rail.base import AgentRail

    # 不存进 ctx.extra（它可能跨节点共享），令牌绑定本次模型输入对象。
    tokens = ContextVar(f"jiuwen_llm_rail_{id(adapter)}", default=None)

    clear_scope, restore_scope = adapter.clear_scope, adapter.restore_scope

    class LlmScopeRail(AgentRail):
        priority = 1_000_000

        async def before_model_call(self, ctx):
            previous = tokens.get()
            cleared = clear_scope()
            tokens.set((id(ctx), cleared, previous))
            try:
                adapter.enter_scope(ctx)
            except Exception:
                raise AbortError(
                    "模型调用缺少可信安全上下文", cause=SecurityDenied("模型调用已拒绝")
                ) from None

        async def after_model_call(self, ctx):
            state = tokens.get()
            if state is not None and state[0] == id(ctx):
                restore_scope(state[1])
                tokens.set(state[2])

    return LlmScopeRail()


def register_provider(adapter):
    """独立公开 provider 名称，不覆盖内置 OpenAI，也不替换其私有客户端。"""
    from openai import AsyncOpenAI
    from openjiuwen.core.common.clients import get_client_registry
    from openjiuwen.core.foundation.llm import (
        AssistantMessage,
        AssistantMessageChunk,
        ToolCall,
        UsageMetadata,
    )
    from openjiuwen.core.foundation.llm.model_clients.base_model_client import BaseModelClient

    provider_id = "jiuwen_guarded_llm_" + uuid4().hex

    class GuardedChatClient(BaseModelClient):
        __client_name__ = None  # 只通过下面的显式公开工厂注册，导入不自动改写注册表。

        def __init__(self, model_config, model_client_config):
            super().__init__(model_config, model_client_config)
            endpoint = chat_endpoint(model_client_config.api_base)
            self._target_ids = frozenset(
                row.target_id for row in adapter.targets.values() if row.endpoint == endpoint
            )
            self._auth_headers = {"authorization": "Bearer " + model_client_config.api_key}

        def _validate_config(self):
            if not self.model_client_config.api_base or not self.model_client_config.api_key:
                raise SecurityDenied("模型客户端身份与目标配置缺失")
            if self.model_client_config.verify_ssl is not True:
                raise SecurityDenied("受保护客户端不关闭 TLS 校验")
            if self.model_client_config.custom_headers:
                raise SecurityDenied("自定义模型认证/路由头尚未验证")

        def _messages(self, messages):
            if isinstance(messages, str):
                return [{"role": "user", "content": messages}]
            result = []
            for message in messages:
                data = (
                    dict(message)
                    if isinstance(message, dict)
                    else message.model_dump(exclude_none=True)
                )
                converted = {
                    key: data[key]
                    for key in ("role", "content", "name", "tool_call_id")
                    if key in data
                }
                if data.get("tool_calls"):
                    converted["tool_calls"] = [
                        call
                        if "function" in call
                        else {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                        for call in data["tool_calls"]
                    ]
                result.append(converted)
            return result

        @asynccontextmanager
        async def _completion(self, messages, *, tools=None, model=None, streaming=False, **kwargs):
            allowed = {"temperature", "top_p", "max_tokens", "stop", "timeout", "output_parser"}
            if set(kwargs) - allowed or kwargs.get("output_parser") is not None:
                raise SecurityDenied("模型调用参数或输出解析器尚未覆盖")
            body = {
                "model": model or self.model_config.model_name,
                "messages": self._messages(messages),
            }
            if streaming:
                body["stream"] = True
                body["stream_options"] = {"include_usage": True}
            for name in ("temperature", "top_p", "max_tokens", "stop"):
                value = kwargs.get(name)
                if value is None:
                    value = getattr(self.model_config, name, None)
                if value is not None:
                    body[name] = value
            if tools:
                body["tools"] = []
                for tool in tools:
                    data = dict(tool) if isinstance(tool, dict) else tool.model_dump()
                    body["tools"].append(
                        data
                        if "function" in data
                        else {
                            "type": "function",
                            "function": {
                                key: data[key] for key in ("name", "description", "parameters")
                            },
                        }
                    )
            timeout = kwargs.get("timeout") or self.model_client_config.timeout
            http = adapter.create_http_client(
                timeout=timeout,
                auth_headers=self._auth_headers,
                target_ids=self._target_ids,
            )
            async with AsyncOpenAI(
                api_key=self.model_client_config.api_key,
                base_url=self.model_client_config.api_base,
                http_client=http,
                timeout=timeout,
                max_retries=self.model_client_config.max_retries,
            ) as client:
                yield await client.chat.completions.create(**body)

        async def invoke(self, messages, *, tools=None, model=None, **kwargs):
            async with self._completion(messages, tools=tools, model=model, **kwargs) as response:
                return self._message(response)

        def _message(self, response):
            selected = response.choices[0]
            usage = response.usage
            return AssistantMessage(
                content=selected.message.content or "",
                finish_reason=selected.finish_reason,
                tool_calls=[
                    ToolCall(
                        id=call.id,
                        type="function",
                        name=call.function.name,
                        arguments=call.function.arguments,
                    )
                    for call in selected.message.tool_calls or []
                ]
                or None,
                usage_metadata=UsageMetadata(
                    model_name=response.model,
                    finish_reason=selected.finish_reason,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                ),
            )

        async def stream(self, messages, *, tools=None, model=None, **kwargs):
            if adapter.results is None:
                raise SecurityDenied("九问流式路径必须安装结果释放门禁")
            async with self._completion(
                messages, tools=tools, model=model, streaming=True, **kwargs
            ) as response:
                async for chunk in response:
                    choice = chunk.choices[0] if chunk.choices else None
                    delta = choice.delta if choice else None
                    yield AssistantMessageChunk(
                        content=delta.content or "" if delta else "",
                        reasoning_content=getattr(delta, "reasoning_content", None)
                        if delta
                        else None,
                        finish_reason=choice.finish_reason or "null" if choice else "null",
                        tool_calls=[
                            ToolCall(
                                id=call.id,
                                type="function",
                                name=call.function.name,
                                arguments=call.function.arguments,
                                index=call.index,
                            )
                            for call in (delta.tool_calls or [])
                        ]
                        if delta
                        else None,
                        usage_metadata=UsageMetadata(
                            model_name=chunk.model,
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                        )
                        if chunk.usage
                        else None,
                    )

        async def generate_image(self, *args, **kwargs):
            raise SecurityDenied("图像模型出口尚未接入")

        async def generate_speech(self, *args, **kwargs):
            raise SecurityDenied("语音模型出口尚未接入")

        async def generate_video(self, *args, **kwargs):
            raise SecurityDenied("视频模型出口尚未接入")

    def factory(model_config, model_client_config):
        return GuardedChatClient(model_config, model_client_config)

    registry = get_client_registry()

    def release():
        if f"llm_{provider_id}" in registry.list_clients():
            registry.unregister(provider_id, "llm")

    def register():
        registry.register_client(provider_id, "llm")(factory)

    adapter.own_provider(register, release)
    return provider_id
