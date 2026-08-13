"""DashScopeLLM — 阿里云 DashScope OpenAI-compatible Chat 实现。"""

from __future__ import annotations

from jiuwen_memory.common._support import read_outbound_ssl, require_ca_file, require_https
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LlmProducer

from .openai_llm import OpenAILLM

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_NONE_VALUES = {"", "none", "null"}


def _parse_enable_thinking(value: object) -> bool | None:
    """解析 DashScope 思考开关；None 表示完全不发送厂商字段。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        if normalized in _NONE_VALUES:
            return None
    raise ValidationError(
        "dashscope params.enable_thinking 必须为 true/false/null"
        f"（当前值：{value!r}）"
    )


class DashScopeLLM(OpenAILLM):
    """DashScope Adapter：默认关闭思考模式；继承 OpenAILLM 的 ConfigSource 晚绑定。

    额外参数：
        enable_thinking: 是否在请求里带 ``extra_body.enable_thinking``；
                         ``None`` 表示完全不发送该厂商字段。
        config_source / config_namespace: 与父类相同，走 ``llm.*`` 晚绑定 key。
    """

    def __init__(
        self,
        model_name: str = "qwen-plus",
        base_url: str | None = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key: str = "",
        default_temperature: float = 0.0,
        default_max_tokens: int = 4096,
        enable_thinking: bool | None = False,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source=None,
        config_namespace: str = "llm",
    ) -> None:
        super().__init__(
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
            ssl_verify=ssl_verify,
            ssl_ca_cert=ssl_ca_cert,
            config_source=config_source,
            config_namespace=config_namespace,
        )
        self._enable_thinking = enable_thinking

    def _provider_request_options(self) -> dict[str, object]:
        if self._enable_thinking is None:
            return {}
        return {"extra_body": {"enable_thinking": self._enable_thinking}}


@LlmProducer.register("dashscope")
def _build(config):
    """装配 DashScope LLM，并挂接内核共享 ConfigSource（若已装配）。"""
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    base_url = (
        config.get("llm_base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    ssl = read_outbound_ssl(config, "llm")
    if ssl.verify:
        require_https(base_url, component="dashscope LLM", param="llm")
        require_ca_file(ssl.ca_cert, component="dashscope LLM", param="llm")
    return DashScopeLLM(
        model_name=config.get("llm_model") or "qwen-plus",
        base_url=base_url,
        api_key=config.get("llm_api_key") or "",
        default_temperature=config.get("llm_temperature", 0.0),
        default_max_tokens=config.get("llm_max_tokens", 4096),
        enable_thinking=_parse_enable_thinking(config.get("enable_thinking", False)),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
    )
