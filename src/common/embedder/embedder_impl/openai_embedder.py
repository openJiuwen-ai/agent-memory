"""OpenAIEmbedder — OpenAI 兼容 Embeddings API 实现。

调用 OpenAI SDK 的 embeddings.create 接口，支持：
  - 官方 OpenAI API
  - vLLM / LocalAI 等自部署 OpenAI 兼容后端（通过 base_url 指定）
  - Matryoshka 维度截断（text-embedding-3 系列支持 dimensions 参数）

模型名、API URL、API KEY、向量维度等均通过构造参数或 EmbedderConfig 配置。
"""

from __future__ import annotations

import openai

from common.base import PluginType
from common.embedder.base import EmbedderProducer
from common.errors import HealthCheckError
from common.log import get_logger

from ..base import Embedder

logger = get_logger(__name__)


def _normalize_base_url(base_url: str | None) -> str | None:
    """Accept either an OpenAI API root or the full ``/embeddings`` endpoint."""
    if base_url is None:
        return None
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/embeddings"):
        normalized = normalized[: -len("/embeddings")]
    return normalized or None


def _coerce_ssl_verify(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"embedder_ssl_verify must be a boolean, got {value!r}")


class OpenAIEmbedder(Embedder):
    """OpenAI 兼容 Embeddings API 实现。

    参数说明：
        model_name: 模型名，如 "text-embedding-3-small"、"bge-m3" 等
        base_url: API 地址，None 时使用 OpenAI 官方地址；
                  自部署后端填 http://localhost:8000/v1 等
        api_key: API KEY（必填）；自部署后端可填任意占位值
        dimension: 输出向量维度；text-embedding-3 系列支持 dimensions 截断
        max_batch_size: 单次 API 调用最大文本数（超过自动拆分批次）
    """

    def __init__(
        self,
        model_name: str = "text-embedding-3-small",
        base_url: str | None = None,
        api_key: str = "",
        dimension: int = 1536,
        max_batch_size: int = 2048,
        ssl_verify: bool | str = True,
    ) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._max_batch_size = max_batch_size
        self._ssl_verify = _coerce_ssl_verify(ssl_verify)
        self._http_client = None

        client_kwargs: dict = {"api_key": api_key}
        normalized_base_url = _normalize_base_url(base_url)
        if normalized_base_url is not None:
            client_kwargs["base_url"] = normalized_base_url
        if not self._ssl_verify:
            logger.warning("OpenAIEmbedder: TLS certificate verification is disabled")
            self._http_client = openai.DefaultHttpxClient(verify=False)
            client_kwargs["http_client"] = self._http_client
        self._client = openai.OpenAI(**client_kwargs)

    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        """探活：调用一次 embed 测试 API 可达。"""
        try:
            self._client.embeddings.create(
                model=self._model_name,
                input="health check",
            )
        except Exception as exc:
            raise HealthCheckError(f"Embedder health check failed: {exc}") from exc

    def dimension(self) -> int:
        """返回配置的输出向量维度。"""
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化：每条输入产出一个向量，顺序与输入一致。

        超过 max_batch_size 时自动拆分批次调用，保证不超出 API 限制。
        """
        if not texts:
            return []

        # 分批
        all_vectors: list[list[float]] = []
        for batch in self._split_batches(texts):
            vectors = self._embed_batch(batch)
            all_vectors.extend(vectors)

        return all_vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """单批次调用 embeddings API。"""
        create_kwargs: dict = {
            "model": self._model_name,
            "input": texts,
        }
        # 支持 Matryoshka 维度截断的模型走 API 原生 dimensions 参数（质量优于手动切片）：
        #   - OpenAI text-embedding-3-*
        #   - 阿里云百炼 text-embedding-v3 / v4
        # 不支持的模型不传 dimensions，按返回全维度向量；
        # 配置维度小于返回维度时手动截断。
        if self._supports_dimensions():
            create_kwargs["dimensions"] = self._dimension

        try:
            response = self._client.embeddings.create(**create_kwargs)
        except openai.APIError as exc:
            logger.error("OpenAIEmbedder: API error — %s", exc)
            raise
        except openai.APIConnectionError as exc:
            logger.error("OpenAIEmbedder: connection error — %s", exc)
            raise

        # 按索引排序保证顺序与输入一致
        sorted_data = sorted(response.data, key=lambda d: d.index)
        vectors = [d.embedding for d in sorted_data]

        # 不支持 dimensions 的模型：API 返回全维度向量，若配置维度小于返回维度则手动截断
        if not self._supports_dimensions() and len(vectors) > 0:
            if len(vectors[0]) > self._dimension:
                dimension = self._dimension
                vectors = [v[:dimension] for v in vectors]

        return vectors

    def _supports_dimensions(self) -> bool:
        """该模型是否支持 API 原生 dimensions 参数（Matryoshka 截断）。

        OpenAI text-embedding-3-* 与阿里云百炼 text-embedding-v3/v4 均支持；
        其他模型默认不支持（按全维度返回，必要时手动截断）。
        """
        import re

        name = self._model_name.lower()
        # OpenAI text-embedding-3-large/small
        if re.search(r"text-embedding-3", name):
            return True
        # 阿里云百炼 text-embedding-v3 / v4 / ...（compatible-mode）
        if re.search(r"text-embedding-v[3-9]", name):
            return True
        return False

    def _split_batches(self, texts: list[str]) -> list[list[str]]:
        """按 max_batch_size 拆分输入列表为多个批次。"""
        batches = []
        for i in range(0, len(texts), self._max_batch_size):
            batch_end = i + self._max_batch_size
            batches.append(texts[i:batch_end])
        return batches


# -- 注册到 EmbedderProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@EmbedderProducer.register("openai")
def _build(config):
    return OpenAIEmbedder(
        model_name=config.get("embedder_model") or "text-embedding-3-small",
        base_url=config.get("embedder_base_url", ""),
        api_key=config.get("embedder_api_key") or "",
        dimension=config.get("embedder_dim", 64),
        max_batch_size=config.get("embedder_max_batch") or 2048,
        ssl_verify=config.get("embedder_ssl_verify", True),
    )
