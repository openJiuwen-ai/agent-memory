# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""构建层 LLM 统一重试入口单测：超时是确定性失败，不重试（BUG2026092802777）。

3x300s=900s 无响应的根因之一就是超时叠加重试；重试分叉只认 LLM 契约的
LLMTimeoutError（由各适配器转换）与内建 TimeoutError，不感知具体 SDK。
"""

import logging

import pytest

from jiuwen_memory.common.errors import LLMTimeoutError
from jiuwen_memory.construction.llm_retry import call_llm_with_retry, is_timeout_error

pytestmark = pytest.mark.unit


class _ScriptedChat:
    """按脚本抛异常/返回值的 chat 桩，记录调用次数（超出脚本重复最后一项）。"""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, messages, **options):
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _logger() -> logging.Logger:
    return logging.getLogger("test_llm_retry")


def test_llm_timeout_error_not_retried() -> None:
    """契约超时（适配器转换后的 LLMTimeoutError）只调 1 次，原样抛出。"""
    chat = _ScriptedChat([LLMTimeoutError("contract timeout")])
    with pytest.raises(LLMTimeoutError):
        call_llm_with_retry(chat, [], max_attempts=3, backoff_ms=0,
                            logger=_logger(), label="T")
    assert chat.calls == 1


def test_builtin_timeout_not_retried() -> None:
    """内建 TimeoutError 兜底同样不重试。"""
    chat = _ScriptedChat([TimeoutError("builtin timeout")])
    with pytest.raises(TimeoutError):
        call_llm_with_retry(chat, [], max_attempts=3, backoff_ms=0,
                            logger=_logger(), label="T")
    assert chat.calls == 1


def test_timeout_subclass_not_retried() -> None:
    class PluginTimeout(TimeoutError):
        """模拟未转换的插件超时（内建 TimeoutError 子类兜底捕获）。"""

    chat = _ScriptedChat([PluginTimeout()])
    with pytest.raises(PluginTimeout):
        call_llm_with_retry(chat, [], max_attempts=3, backoff_ms=0,
                            logger=_logger(), label="T")
    assert chat.calls == 1


def test_non_timeout_retried_up_to_max_attempts() -> None:
    chat = _ScriptedChat([ValueError("transient")])
    with pytest.raises(ValueError):
        call_llm_with_retry(chat, [], max_attempts=3, backoff_ms=0,
                            logger=_logger(), label="T")
    assert chat.calls == 3


def test_recovers_after_transient_failure() -> None:
    chat = _ScriptedChat([ValueError("blip"), "ok"])
    assert call_llm_with_retry(chat, [], max_attempts=3, backoff_ms=0,
                               logger=_logger(), label="T") == "ok"
    assert chat.calls == 2


def test_options_forwarded_to_chat() -> None:
    seen: dict = {}

    def chat(messages, **options):
        seen.update(options)
        return "ok"

    call_llm_with_retry(chat, [], max_attempts=1, backoff_ms=0,
                        logger=_logger(), label="T",
                        temperature=0, max_tokens=4096)
    assert seen == {"temperature": 0, "max_tokens": 4096}


def test_is_timeout_error_contract() -> None:
    assert is_timeout_error(LLMTimeoutError("x"))
    assert is_timeout_error(TimeoutError("x"))
    assert not is_timeout_error(ValueError("x"))
