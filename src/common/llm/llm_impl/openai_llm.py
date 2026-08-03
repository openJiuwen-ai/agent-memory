"""OpenAILLM — OpenAI 兼容 Chat Completions API 实现。

调用 OpenAI SDK 的 chat.completions.create 接口，支持：
  - 官方 OpenAI API
  - vLLM / LocalAI 等自部署 OpenAI 兼容后端（通过 base_url 指定）

模型名、URL、API KEY 等均通过构造参数或 LLMConfig 配置。
"""

from __future__ import annotations

from collections.abc import Mapping

import openai

from common._support import (
    outbound_verify,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from common.base import PluginType
from common.errors import HealthCheckError
from common.llm.base import LlmProducer
from common.log import get_logger
from common.type_def import ChatMessage

from ..base import LLM

logger = get_logger(__name__)


class OpenAILLM(LLM):
    """OpenAI 兼容 Chat Completions API 实现。

    参数说明：
        model_name: 模型名，如 "gpt-4o"、"claude-sonnet-4-6" 等
        base_url: API 地址，None 时使用 OpenAI 官方地址；
                  自部署后端填 http://localhost:8000/v1 等
        api_key: API KEY（必填）；自部署后端可填任意占位值
        default_temperature: 默认生成温度（0.0 = 确定性输出）
        default_max_tokens: 默认最大生成 token 数
    """

    def __init__(
        self,
        model_name: str = "gpt-4o",
        base_url: str | None = None,
        api_key: str = "",
        default_temperature: float = 0.0,
        default_max_tokens: int = 4096,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._api_key = api_key
        self._base_url = base_url or None  # 空串视作未配置 → 用官方端点
        # ssl_verify 只决定是否接管信任锚：关闭时完全不干预（http 明文直连、https
        # 仍走 SDK 默认的公共 CA 校验）；开启时按 ssl_ca_cert 覆盖，缺省回落系统 CA。
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = (ssl_ca_cert or "").strip() or None
        self._client = None  # 惰性创建（见 client 属性）

    @property
    def client(self) -> "openai.OpenAI":
        """
        惰性创建 OpenAI 客户端：装配期不连、不校验凭证——缺 key 也能装配成功，
        只有真正 chat/health 时才要求凭证（与存储后端惰性连接的约定一致）。
        """
        if self._client is None:
            client_kwargs: dict = {"api_key": self._api_key}
            if self._base_url:
                client_kwargs["base_url"] = self._base_url
            if self._ssl_verify:
                # 使用 SDK 提供的默认客户端，在注入信任锚的同时保留其
                # 长读取超时、连接池和重定向等默认参数。
                client_kwargs["http_client"] = openai.DefaultHttpxClient(
                    verify=outbound_verify(self._ssl_ca_cert)
                )
            self._client = openai.OpenAI(**client_kwargs)
        return self._client

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def _provider_request_options(self) -> dict[str, object]:
        """返回 Provider Adapter 的默认请求选项。

        通用 OpenAI 实现不注入任何厂商字段；兼容协议上的厂商差异由子类覆盖。
        """
        return {}

    def _merge_request_options(
        self,
        create_kwargs: dict[str, object],
        options: Mapping[str, object],
    ) -> None:
        """先合并 Provider 默认值，再让显式调用参数覆盖。"""
        create_kwargs.update(self._provider_request_options())
        for key, value in options.items():
            if key in ("temperature", "max_tokens") or value is None:
                continue
            if (
                key == "extra_body"
                and isinstance(create_kwargs.get(key), Mapping)
                and isinstance(value, Mapping)
            ):
                create_kwargs[key] = {**create_kwargs[key], **value}
            else:
                create_kwargs[key] = value

    def health(self) -> None:
        """探活：调用一次极短 chat 测试 API 可达。"""
        try:
            create_kwargs: dict[str, object] = {
                "model": self._model_name,
                "messages": [{"role": "user", "content": "health check"}],
                "max_tokens": 1,
            }
            create_kwargs.update(self._provider_request_options())
            self.client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            raise HealthCheckError(f"LLM health check failed: {exc}") from exc

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        """执行一次对话补全，返回助手回复文本。

        ``options`` 携带生成参数（temperature、max_tokens 等），
        未指定时使用构造参数中的默认值。后端忽略不认识的键。
        """
        # 转换 ChatMessage → OpenAI dict 格式
        oa_messages = [{"role": m.role, "content": m.content} for m in messages]

        # 合合默认参数 + 调用方 override
        temperature = options.get("temperature", self._default_temperature)
        max_tokens = options.get("max_tokens", self._default_max_tokens)

        create_kwargs: dict[str, object] = {
            "model": self._model_name,
            "messages": oa_messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }

        # 透传其他 options（如 top_p、stop、response_format 等）。
        self._merge_request_options(create_kwargs, options)

        try:
            response = self.client.chat.completions.create(**create_kwargs)
        except openai.APIError as exc:
            logger.error("OpenAILLM: API error — %s", exc)
            raise
        except openai.APIConnectionError as exc:
            logger.error("OpenAILLM: connection error — %s", exc)
            raise

        content = response.choices[0].message.content
        return content or ""


# -- 注册到 LlmProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@LlmProducer.register("openai")
def _build(config):
    base_url = config.get("llm_base_url", "")
    ssl = read_outbound_ssl(config, "llm")
    if ssl.verify:
        require_https(base_url, component="openai LLM", param="llm")
        require_ca_file(ssl.ca_cert, component="openai LLM", param="llm")
    return OpenAILLM(
        model_name=config.get("llm_model") or "gpt-4o",
        base_url=base_url,
        api_key=config.get("llm_api_key") or "",
        default_temperature=config.get("llm_temperature", 0.0),
        default_max_tokens=config.get("llm_max_tokens", 4096),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
    )
