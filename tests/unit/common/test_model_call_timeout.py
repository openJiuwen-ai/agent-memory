"""OpenAI LLM / Embedder 出站调用等待策略测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from jiuwen_memory.common._support import (
    DEFAULT_OUTBOUND_MAX_RETRIES,
    DEFAULT_OUTBOUND_TIMEOUT_SECONDS,
    OUTBOUND_CONNECT_TIMEOUT_SECONDS,
    OutboundCallPolicy,
    read_outbound_call_policy,
)
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.embedder_impl import EmbedderProducer
from jiuwen_memory.common.embedder.embedder_impl.openai_embedder import OpenAIEmbedder
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.llm_impl import LlmProducer
from jiuwen_memory.common.llm.llm_impl.openai_llm import OpenAILLM
from jiuwen_memory.common.type_def import ChatMessage
from jiuwen_memory.config import AssemblyContext


def _record_openai_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 OpenAI SDK 客户端工厂并记录构造参数。"""
    recorded: dict[str, Any] = {}

    def _factory(**kwargs: Any) -> object:
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(openai, "OpenAI", _factory)
    return recorded


def test_read_outbound_call_policy_defaults_are_overridable() -> None:
    """调用策略函数的默认参数应与系统默认值一致，且可显式覆盖。"""
    policy = read_outbound_call_policy({}, "llm")
    assert policy == OutboundCallPolicy(
        DEFAULT_OUTBOUND_TIMEOUT_SECONDS, DEFAULT_OUTBOUND_MAX_RETRIES
    )

    policy = read_outbound_call_policy({}, "llm", default_timeout=5, default_max_retries=1)
    assert policy == OutboundCallPolicy(5.0, 1)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"llm_timeout": "bad"}, "llm_timeout must be a positive number; got 'bad'"),
        ({"llm_timeout": "inf"}, "llm_timeout must be a finite positive number; got 'inf'"),
        (
            {"llm_max_retries": 1.5},
            "llm_max_retries must be a non-negative integer; got 1.5",
        ),
    ],
)
def test_read_outbound_call_policy_uses_english_validation_messages(
    config: dict[str, Any], message: str
) -> None:
    """出站调用策略的校验异常应使用英文。"""
    with pytest.raises(ValidationError) as exc_info:
        read_outbound_call_policy(config, "llm")

    assert str(exc_info.value) == message, "校验异常应提供稳定的英文信息"


def test_openai_llm_default_call_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 默认 300s 有界等待、connect 固定 5s、不做 SDK 内部重试。"""
    recorded = _record_openai_client(monkeypatch)
    llm = LlmProducer.build("openai", {"llm_api_key": "key"}, AssemblyContext())
    assert llm.client is not None, "访问 client 应触发 OpenAI 客户端初始化"

    timeout = recorded["timeout"]
    assert timeout.read == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认读取超时应为 300s"
    assert timeout.write == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认写入超时应为 300s"
    assert timeout.pool == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认连接池等待应为 300s"
    assert timeout.connect == OUTBOUND_CONNECT_TIMEOUT_SECONDS, "连接超时应固定为 5s"
    assert recorded["max_retries"] == DEFAULT_OUTBOUND_MAX_RETRIES, "默认不得叠加 SDK 自动重试"


def test_openai_llm_custom_call_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 配置值可覆盖默认策略，并兼容装配展开出的数字字符串。"""
    recorded = _record_openai_client(monkeypatch)
    llm = LlmProducer.build(
        "openai",
        {"llm_api_key": "key", "llm_timeout": "45", "llm_max_retries": "2"},
        AssemblyContext(),
    )
    assert llm.client is not None, "访问 client 应触发 OpenAI 客户端初始化"

    timeout = recorded["timeout"]
    assert timeout.read == 45.0, "llm_timeout 应传给 SDK timeout.read"
    assert timeout.connect == OUTBOUND_CONNECT_TIMEOUT_SECONDS, "llm_timeout 不得覆盖 connect 超时"
    assert recorded["max_retries"] == 2, "llm_max_retries 应传给 SDK"


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", "bad"])
def test_openai_llm_rejects_invalid_timeout(value: Any) -> None:
    """非法 llm_timeout 必须在装配期失败。"""
    with pytest.raises(ValidationError):
        LlmProducer.build("openai", {"llm_api_key": "key", "llm_timeout": value}, AssemblyContext())


@pytest.mark.parametrize("value", [-1, 1.5, True, "bad"])
def test_openai_llm_rejects_invalid_max_retries(value: Any) -> None:
    """非法 llm_max_retries 必须在装配期失败。"""
    with pytest.raises(ValidationError):
        LlmProducer.build(
            "openai", {"llm_api_key": "key", "llm_max_retries": value}, AssemblyContext()
        )


def test_openai_embedder_default_call_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedder 默认 300s 有界等待、connect 固定 5s、不做 SDK 内部重试。"""
    recorded = _record_openai_client(monkeypatch)
    embedder = EmbedderProducer.build("openai", {"embedder_api_key": "key"}, AssemblyContext())
    assert embedder.client is not None, "访问 client 应触发 OpenAI 客户端初始化"

    timeout = recorded["timeout"]
    assert timeout.read == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认读取超时应为 300s"
    assert timeout.write == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认写入超时应为 300s"
    assert timeout.pool == DEFAULT_OUTBOUND_TIMEOUT_SECONDS, "默认连接池等待应为 300s"
    assert timeout.connect == OUTBOUND_CONNECT_TIMEOUT_SECONDS, "连接超时应固定为 5s"
    assert recorded["max_retries"] == DEFAULT_OUTBOUND_MAX_RETRIES, "默认不得叠加 SDK 自动重试"


def test_openai_embedder_custom_call_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedder 配置值可覆盖默认策略，并兼容装配展开出的数字字符串。"""
    recorded = _record_openai_client(monkeypatch)
    embedder = EmbedderProducer.build(
        "openai",
        {"embedder_api_key": "key", "embedder_timeout": "45", "embedder_max_retries": "2"},
        AssemblyContext(),
    )
    assert embedder.client is not None, "访问 client 应触发 OpenAI 客户端初始化"

    timeout = recorded["timeout"]
    assert timeout.read == 45.0, "embedder_timeout 应传给 SDK timeout.read"
    assert timeout.connect == OUTBOUND_CONNECT_TIMEOUT_SECONDS, (
        "embedder_timeout 不得覆盖 connect 超时"
    )
    assert recorded["max_retries"] == 2, "embedder_max_retries 应传给 SDK"


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", "bad"])
def test_openai_embedder_rejects_invalid_timeout(value: Any) -> None:
    """非法 embedder_timeout 必须在装配期失败。"""
    with pytest.raises(ValidationError):
        EmbedderProducer.build(
            "openai",
            {"embedder_api_key": "key", "embedder_timeout": value},
            AssemblyContext(),
        )


@pytest.mark.parametrize("value", [-1, 1.5, True, "bad"])
def test_openai_embedder_rejects_invalid_max_retries(value: Any) -> None:
    """非法 embedder_max_retries 必须在装配期失败。"""
    with pytest.raises(ValidationError):
        EmbedderProducer.build(
            "openai",
            {"embedder_api_key": "key", "embedder_max_retries": value},
            AssemblyContext(),
        )


def test_openai_llm_start_log_omits_content(caplog) -> None:
    """LLM 发起日志只带调用元数据，不得带 message 正文。"""
    llm = OpenAILLM(api_key="mock-key")
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    llm.client.chat.completions.create = MagicMock(return_value=response)

    with caplog.at_level("INFO", logger="agent_memory.common.llm.llm_impl.openai_llm"):
        result = llm.chat([ChatMessage(role="user", content="secret prompt")])

    assert result == "ok"
    assert "model=gpt-4o" in caplog.text, "发起日志应包含模型名"
    assert f"timeout={DEFAULT_OUTBOUND_TIMEOUT_SECONDS:.3f}s" in caplog.text, (
        "发起日志应包含超时配置"
    )
    assert f"max_retries={DEFAULT_OUTBOUND_MAX_RETRIES}" in caplog.text, "发起日志应包含重试配置"
    assert "secret prompt" not in caplog.text, "发起日志不得包含用户正文"


def test_openai_llm_timeout_log(caplog, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 超时异常单独记录后原样抛出。"""

    class FakeAPITimeoutError(Exception):
        pass

    monkeypatch.setattr(openai, "APITimeoutError", FakeAPITimeoutError)
    llm = OpenAILLM(api_key="mock-key")
    llm.client.chat.completions.create = MagicMock(side_effect=FakeAPITimeoutError)

    with (
        caplog.at_level("ERROR", logger="agent_memory.common.llm.llm_impl.openai_llm"),
        pytest.raises(FakeAPITimeoutError),
    ):
        llm.chat([ChatMessage(role="user", content="secret prompt")])

    assert "timed out" in caplog.text, "超时应单独记录"
    assert f"timeout={DEFAULT_OUTBOUND_TIMEOUT_SECONDS:.3f}s" in caplog.text, (
        "超时日志应包含配置上限"
    )
    assert "secret prompt" not in caplog.text, "超时日志不得包含用户正文"


def test_openai_embedder_start_log_omits_text(caplog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedder 发起日志只带批量元数据，不得带输入文本。"""
    embedder = OpenAIEmbedder(api_key="mock-key", dimension=4)
    mock_client = MagicMock()
    response = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.0] * 4)])
    mock_client.embeddings.create = MagicMock(return_value=response)
    monkeypatch.setattr(embedder, "_client", mock_client)
    monkeypatch.setattr(embedder, "_client_fingerprint", ("mock-key", None, False, None))

    with caplog.at_level(
        "INFO", logger="agent_memory.common.embedder.embedder_impl.openai_embedder"
    ):
        result = embedder.embed(["secret text"])

    assert result == [[0.0] * 4]
    assert "model=text-embedding-3-small" in caplog.text, "发起日志应包含模型名"
    assert "items=1" in caplog.text, "发起日志应包含批量大小"
    assert f"timeout={DEFAULT_OUTBOUND_TIMEOUT_SECONDS:.3f}s" in caplog.text, (
        "发起日志应包含超时配置"
    )
    assert "secret text" not in caplog.text, "发起日志不得包含输入文本"


def test_openai_embedder_timeout_log(caplog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedder 超时异常单独记录后原样抛出。"""

    class FakeAPITimeoutError(Exception):
        pass

    monkeypatch.setattr(openai, "APITimeoutError", FakeAPITimeoutError)
    embedder = OpenAIEmbedder(api_key="mock-key", dimension=4)
    mock_client = MagicMock()
    mock_client.embeddings.create = MagicMock(side_effect=FakeAPITimeoutError)
    monkeypatch.setattr(embedder, "_client", mock_client)
    monkeypatch.setattr(embedder, "_client_fingerprint", ("mock-key", None, False, None))

    with (
        caplog.at_level(
            "ERROR", logger="agent_memory.common.embedder.embedder_impl.openai_embedder"
        ),
        pytest.raises(FakeAPITimeoutError),
    ):
        embedder.embed(["secret text"])

    assert "timed out" in caplog.text, "超时应单独记录"
    assert "items=1" in caplog.text, "超时日志应包含批量大小"
    assert "secret text" not in caplog.text, "超时日志不得包含输入文本"


def test_openai_plugin_types() -> None:
    """两个插件类型声明保持不变。"""
    assert OpenAILLM(api_key="key").plugin_type() == PluginType.LLM
    assert OpenAIEmbedder(api_key="key").plugin_type() == PluginType.EMBEDDER


def test_dashscope_custom_call_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """DashScope 复用 OpenAI 客户端，也必须读取 llm 等待策略。"""
    recorded = _record_openai_client(monkeypatch)
    llm = LlmProducer.build(
        "dashscope",
        {"llm_api_key": "key", "llm_timeout": "45", "llm_max_retries": "2"},
        AssemblyContext(),
    )
    assert llm.client is not None, "访问 client 应触发 OpenAI 客户端初始化"

    timeout = recorded["timeout"]
    assert timeout.read == 45.0, "DashScope 应读取 llm_timeout"
    assert timeout.connect == OUTBOUND_CONNECT_TIMEOUT_SECONDS, "DashScope 不得覆盖 connect 超时"
    assert recorded["max_retries"] == 2, "DashScope 应读取 llm_max_retries"
