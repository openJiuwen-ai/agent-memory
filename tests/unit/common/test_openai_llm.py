"""OpenAILLM 单元测试（10 个测试）。

使用 Mock 替换 openai client，隔离外部 API 依赖。
"""

from unittest.mock import MagicMock

import pytest

from common.base import PluginType
from common.errors import HealthCheckError
from common.llm.llm_impl import LlmProducer
from common.llm.llm_impl.openai_llm import OpenAILLM
from common.type_def import ChatMessage

# ---------------------------------------------------------------------------
# Helper: Mock openai client
# ---------------------------------------------------------------------------


class MockCompletionChoice:
    """Mock chat.completions.create 返回中的 choice。"""

    def __init__(self, content: str) -> None:
        self.message = MagicMock()
        self.message.content = content


class MockCompletionResponse:
    """Mock chat.completions.create 返回对象。"""

    def __init__(self, content: str) -> None:
        self.choices = [MockCompletionChoice(content)]


def _make_mock_llm(
    default_content: str = "mock response",
    model_name: str = "gpt-4o",
    default_temperature: float = 0.0,
    default_max_tokens: int = 4096,
) -> OpenAILLM:
    """创建使用 Mock client 的 OpenAILLM。"""
    llm = OpenAILLM(
        model_name=model_name,
        api_key="mock-key",
        default_temperature=default_temperature,
        default_max_tokens=default_max_tokens,
    )
    mock_create = MagicMock(return_value=MockCompletionResponse(default_content))
    # 访问 .client 触发惰性建客户端（api_key 已给），再替换其 create 为 mock。
    llm.client.chat.completions.create = mock_create
    return llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plugin_type():
    """T-L-01: plugin_type 返回 LLM。"""
    llm = _make_mock_llm()
    assert llm.plugin_type() == PluginType.LLM


def test_chat_basic():
    """T-L-02: 单轮对话返回助手回复文本。"""
    llm = _make_mock_llm(default_content="hello assistant")
    messages = [
        ChatMessage(role="user", content="hello"),
    ]
    result = llm.chat(messages)
    assert result == "hello assistant"


def test_chat_multi_turn():
    """T-L-03: 多轮对话：system + user → 返回助手回复。"""
    llm = _make_mock_llm(default_content="I prefer Python")
    messages = [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="What language do you prefer?"),
    ]
    result = llm.chat(messages)
    assert result == "I prefer Python"
    call_kwargs = llm.client.chat.completions.create.call_args
    oa_messages = call_kwargs.kwargs["messages"]
    assert oa_messages[0]["role"] == "system"
    assert oa_messages[1]["role"] == "user"


def test_chat_options_override():
    """T-L-04: options 参数可覆盖默认 temperature / max_tokens。"""
    llm = _make_mock_llm(default_temperature=0.0, default_max_tokens=4096)
    llm.chat([ChatMessage(role="user", content="test")], temperature=0.7, max_tokens=100)

    call_kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0.7
    assert call_kwargs["max_tokens"] == 100


def test_chat_default_params():
    """T-L-05: 无 options 时使用构造参数的默认值。"""
    llm = _make_mock_llm(default_temperature=0.5, default_max_tokens=2048)
    llm.chat([ChatMessage(role="user", content="test")])

    call_kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0.5
    assert call_kwargs["max_tokens"] == 2048
    assert "extra_body" not in call_kwargs


def test_generate():
    """T-L-06: generate 单 prompt 便捷方法。"""
    llm = _make_mock_llm(default_content="generated text")
    result = llm.generate("test prompt")
    assert result == "generated text"
    call_kwargs = llm.client.chat.completions.create.call_args.kwargs
    oa_messages = call_kwargs["messages"]
    assert len(oa_messages) == 1
    assert oa_messages[0]["role"] == "user"
    assert oa_messages[0]["content"] == "test prompt"


def test_health():
    """T-L-07: health 正常时返回 None。"""
    llm = _make_mock_llm()
    result = llm.health()
    assert result is None
    assert "extra_body" not in llm.client.chat.completions.create.call_args.kwargs


def test_health_failure():
    """T-L-08: health 失败时抛 HealthCheckError。"""
    llm = _make_mock_llm()
    llm.client.chat.completions.create = MagicMock(side_effect=Exception("API down"))
    with pytest.raises(HealthCheckError):
        llm.health()


# ---------------------------------------------------------------------------
# Producer tests
# ---------------------------------------------------------------------------


def test_producer_known():
    """T-P-01: LlmProducer 已注册内置 LLM Provider。"""
    assert "openai" in LlmProducer.known()
    assert "dashscope" in LlmProducer.known()
    assert "echo" in LlmProducer.known()


def test_producer_unknown_type():
    """T-P-02: 不支持的 llm_type 抛 ValidationError。"""
    from common.errors import ValidationError
    from config import AssemblyContext

    with pytest.raises(ValidationError):
        LlmProducer.build("unknown", {}, AssemblyContext())
