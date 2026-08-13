"""DashScopeLLM Provider Adapter 的单元测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.llm_impl import LlmProducer
from jiuwen_memory.common.llm.llm_impl.dashscope_llm import DashScopeLLM
from jiuwen_memory.common.type_def import ChatMessage
from jiuwen_memory.config import AssemblyContext

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]


def _response(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _mock_llm(**kwargs) -> DashScopeLLM:
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    with patch(
        "jiuwen_memory.common.llm.llm_impl.openai_llm.openai.OpenAI",
        return_value=client,
    ):
        llm = DashScopeLLM(**kwargs)
        assert llm.client is client
    return llm


def _mock_produced_llm(params: dict[str, object]) -> DashScopeLLM:
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    with patch(
        "jiuwen_memory.common.llm.llm_impl.openai_llm.openai.OpenAI",
        return_value=client,
    ):
        llm = LlmProducer.build("dashscope", params, AssemblyContext())
        assert isinstance(llm, DashScopeLLM)
        assert llm.client is client
    return llm


def test_default_disables_thinking_for_chat_and_health() -> None:
    llm = _mock_llm()

    assert llm.chat([ChatMessage(role="user", content="hello")]) == "ok"
    chat_kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert chat_kwargs["extra_body"] == {"enable_thinking": False}

    llm.health()
    health_kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert health_kwargs["extra_body"] == {"enable_thinking": False}


@pytest.mark.parametrize("enable_thinking", [True, False])
def test_explicit_thinking_value_is_forwarded(enable_thinking: bool) -> None:
    llm = _mock_llm(enable_thinking=enable_thinking)

    llm.chat([ChatMessage(role="user", content="hello")])

    kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": enable_thinking}


def test_none_omits_dashscope_vendor_field() -> None:
    llm = _mock_llm(enable_thinking=None)

    llm.chat([ChatMessage(role="user", content="hello")])

    kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs


def test_call_extra_body_can_override_provider_default() -> None:
    llm = _mock_llm()

    llm.chat(
        [ChatMessage(role="user", content="hello")],
        extra_body={"enable_thinking": True, "vendor_option": "value"},
    )

    kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {
        "enable_thinking": True,
        "vendor_option": "value",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("OFF", False),
        ("null", None),
        (None, None),
    ],
)
def test_producer_parses_enable_thinking(raw, expected) -> None:
    llm = _mock_produced_llm({"enable_thinking": raw})

    llm.chat([ChatMessage(role="user", content="hello")])

    kwargs = llm.client.chat.completions.create.call_args.kwargs
    if expected is None:
        assert "extra_body" not in kwargs
    else:
        assert kwargs["extra_body"] == {"enable_thinking": expected}


def test_producer_defaults_to_disabled_thinking() -> None:
    llm = _mock_produced_llm({})

    llm.chat([ChatMessage(role="user", content="hello")])

    kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_producer_rejects_invalid_enable_thinking() -> None:
    with pytest.raises(ValidationError, match="enable_thinking"):
        LlmProducer.build(
            "dashscope",
            {"enable_thinking": "sometimes"},
            AssemblyContext(),
        )


@pytest.mark.parametrize("mode", ["online", "local"])
def test_docker_config_defaults_to_dashscope_with_thinking_disabled(mode: str) -> None:
    config_path = _ROOT / "deploy" / "docker" / mode / "config.yml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))["memory_api"]

    llm_config = payload["llm"]["default"]
    assert llm_config["target"] == "dashscope"
    assert llm_config["params"]["enable_thinking"] == "${LLM_ENABLE_THINKING:-false}"
