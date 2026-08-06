"""OpenAIEmbedder — OpenAI 兼容 Embeddings API 实现。

调用 OpenAI SDK 的 embeddings.create 接口，支持：
  - 官方 OpenAI API
  - vLLM / LocalAI / SiliconFlow 等自部署或兼容后端（通过 base_url 指定）
  - Matryoshka 维度截断（text-embedding-3 系列支持 dimensions 参数）

模型名、API URL、API KEY 优先经 :class:`~config.config_source.ConfigSource`
在每次 ``embed`` / ``health`` 路径晚绑定（S08）；构造参数仅为装配期回落默认值。
凭证变化时重建 OpenAI 客户端。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import openai

from common._support import (
    outbound_verify,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from common.base import PluginType
from common.embedder.base import EmbedderProducer
from common.errors import HealthCheckError
from common.log import get_logger
from config.binding import resolve_endpoint
from config.keys import NS_EMBEDDER

from ..base import Embedder

if TYPE_CHECKING:
    from config.config_source import ConfigSource

logger = get_logger(__name__)


class OpenAIEmbedder(Embedder):
    """OpenAI 兼容 Embeddings API 实现（支持 ConfigSource 晚绑定）。

    参数说明：
        model_name: 模型名回落默认，如 ``"text-embedding-3-small"``、``"bge-m3"``
        base_url: API 地址回落默认；``None`` 时用官方地址；兼容后端填
                  ``http://localhost:8000/v1`` / SiliconFlow 等
        api_key: API KEY 回落默认；自部署后端可填任意占位值
        dimension: 输出向量维度；text-embedding-3 系列支持 dimensions 截断
        max_batch_size: 单次 API 调用最大文本数（超过自动拆分批次）
        ssl_verify / ssl_ca_cert: 出站 TLS 校验（见 ``common._support``）
        config_source: 可选；每次调用 ``fetch("embedder.model|api_key|base_url")``
        config_namespace: ConfigSource key 命名空间，默认 ``embedder``
    """

    def __init__(
        self,
        model_name: str = "text-embedding-3-small",
        base_url: str | None = None,
        api_key: str = "",
        dimension: int = 1536,
        max_batch_size: int = 2048,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source: ConfigSource | None = None,
        config_namespace: str = NS_EMBEDDER,
    ) -> None:
        # 构造期值仅作 ConfigSource 缺失时的回落；运行时以 fetch 为准
        self._fallback_model = model_name
        self._fallback_base_url = base_url or None
        self._fallback_api_key = api_key
        self._dimension = dimension
        self._max_batch_size = max_batch_size
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = (ssl_ca_cert or "").strip() or None
        self._config_source = config_source
        self._config_namespace = config_namespace
        # 惰性创建：装配期不连网；凭证指纹变化时重建
        self._client: openai.OpenAI | None = None
        self._client_fingerprint: tuple[str, str | None, bool, str | None] | None = None

    def plugin_type(self) -> PluginType:
        """返回插件类型 ``EMBEDDER``。"""
        return PluginType.EMBEDDER

    def _endpoint(self):
        """解析当前应生效的 model / api_key / base_url（ConfigSource 优先）。"""
        return resolve_endpoint(
            self._config_source,
            namespace=self._config_namespace,
            fallback_model=self._fallback_model,
            fallback_api_key=self._fallback_api_key,
            fallback_base_url=self._fallback_base_url,
        )

    def _ensure_client(self):
        """按当前晚绑定凭证确保 client；凭证变化时重建。返回 ``(model, client)``。"""
        ep = self._endpoint()
        fingerprint = (ep.api_key, ep.base_url, self._ssl_verify, self._ssl_ca_cert)
        if self._client is None or self._client_fingerprint != fingerprint:
            client_kwargs: dict = {"api_key": ep.api_key}
            if ep.base_url is not None:
                client_kwargs["base_url"] = ep.base_url
            if self._ssl_verify:
                # 使用 SDK 默认客户端，在注入信任锚的同时保留长读取超时、连接池等
                client_kwargs["http_client"] = openai.DefaultHttpxClient(
                    verify=outbound_verify(self._ssl_ca_cert)
                )
            self._client = openai.OpenAI(**client_kwargs)
            self._client_fingerprint = fingerprint
        return ep.model, self._client

    @property
    def client(self) -> openai.OpenAI:
        """惰性创建的 OpenAI 客户端（凭证经 ConfigSource 晚绑定；变化时重建）。"""
        _, client = self._ensure_client()
        return client

    def health(self) -> None:
        """探活：调用一次 embed 测试 API 可达。"""
        try:
            model, client = self._ensure_client()
            client.embeddings.create(model=model, input="health check")
        except Exception as exc:
            raise HealthCheckError(f"Embedder health check failed: {exc}") from exc

    def dimension(self) -> int:
        """返回配置的输出向量维度。"""
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：每条输入产出一个向量，顺序与输入一致。

        超过 ``max_batch_size`` 时自动拆分批次调用，保证不超出 API 限制。
        每次调用重新解析 ConfigSource 上的 model/凭证。
        """
        if not texts:
            return []

        # 分批
        all_vectors: list[list[float]] = []
        for batch in self._split_batches(texts):
            all_vectors.extend(self._embed_batch(batch))
        return all_vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """单批次调用 embeddings API。"""
        model, client = self._ensure_client()
        create_kwargs: dict = {"model": model, "input": texts}
        # 支持 Matryoshka 维度截断的模型走 API 原生 dimensions 参数（质量优于手动切片）：
        #   - OpenAI text-embedding-3-*
        #   - 阿里云百炼 text-embedding-v3 / v4
        # 不支持的模型不传 dimensions，按返回全维度向量；
        # 配置维度小于返回维度时手动截断。
        if self._supports_dimensions(model):
            create_kwargs["dimensions"] = self._dimension
        try:
            response = client.embeddings.create(**create_kwargs)
        except openai.APIError as exc:
            logger.error("OpenAIEmbedder: API error — %s", exc)
            raise
        except openai.APIConnectionError as exc:
            logger.error("OpenAIEmbedder: connection error — %s", exc)
            raise

        # 按索引排序保证顺序与输入一致
        sorted_data = sorted(response.data, key=lambda d: d.index)
        vectors = [d.embedding for d in sorted_data]
        if not self._supports_dimensions(model) and len(vectors) > 0:
            if len(vectors[0]) > self._dimension:
                dimension = self._dimension
                vectors = [v[:dimension] for v in vectors]
        return vectors

    def _supports_dimensions(self, model_name: str | None = None) -> bool:
        """该模型是否支持 API 原生 dimensions 参数（Matryoshka 截断）。

        OpenAI text-embedding-3-* 与阿里云百炼 text-embedding-v3/v4 均支持；
        其他模型默认不支持（按全维度返回，必要时手动截断）。
        """
        import re

        name = (model_name or self._fallback_model).lower()
        if re.search(r"text-embedding-3", name):
            return True
        if re.search(r"text-embedding-v[3-9]", name):
            return True
        return False

    def _split_batches(self, texts: list[str]) -> list[list[str]]:
        """按 max_batch_size 拆分输入列表为多个批次。"""
        batches = []
        for i in range(0, len(texts), self._max_batch_size):
            batches.append(texts[i:i + self._max_batch_size])
        return batches


# -- 注册到 EmbedderProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@EmbedderProducer.register("openai")
def _build(config):
    """从装配 ComponentConfig 构造；注入内核共享的 default ConfigSource（若已装配）。"""
    base_url = config.get("embedder_base_url", "")
    ssl = read_outbound_ssl(config, "embedder")
    if ssl.verify:
        require_https(base_url, component="openai embedder", param="embedder")
        require_ca_file(ssl.ca_cert, component="openai embedder", param="embedder")
    from config.config_source import ConfigSourceProducer

    return OpenAIEmbedder(
        model_name=config.get("embedder_model") or "text-embedding-3-small",
        base_url=base_url,
        api_key=config.get("embedder_api_key") or "",
        dimension=config.get("embedder_dim", 64),
        max_batch_size=config.get("embedder_max_batch") or 2048,
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
    )
