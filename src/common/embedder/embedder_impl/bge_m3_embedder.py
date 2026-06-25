"""BGEM3Embedder — BAAI BGE-M3 本地向量化：多语言（100+）、多功能（dense/sparse/colbert）、
长文本（8192 tokens）。

使用 ``FlagEmbedding`` 库加载本地模型，零外部 API 调用。支持 dense 向量输出
（L2 归一化），维度 1024。自动按 ``max_batch_size`` 分批编码以控制 GPU/CPU 内存。

L2 归一化在 encode 之后手动完成（而非传 normalize_embeddings 给底层 tokenizer），
兼容所有 FlagEmbedding / sentence-transformers / transformers 版本。

加载策略：先离线从本地缓存加载（快速、不联网）；缓存不存在时再联网下载。
本地路径（目录存在）时直接加载，跳过离线/联网逻辑。

依赖：``FlagEmbedding`` + ``sentence-transformers`` + ``torch``（pip install FlagEmbedding）。
"""

from __future__ import annotations

import math
from typing import List

from common.base import PluginType
from common.embedder.base import Embedder, EmbedderProducer
from common.errors import HealthCheckError
from common.log import get_logger

logger = get_logger(__name__)


def _l2_normalize(vec: List[float]) -> List[float]:
    """L2 归一化：将向量缩放为单位长度，兼容归一化需求同时不依赖底层 tokenizer 参数。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class BGEM3Embedder(Embedder):
    """BGE-M3 本地向量化器——多语言 dense embedding。"""

    def __init__(
        self,
        model_name_or_path: str = "BAAI/bge-m3",
        use_fp16: bool = True,
        dimension: int = 1024,
        max_batch_size: int = 12,
        max_length: int = 8192,
        normalize_embeddings: bool = True,
    ) -> None:
        self._model_name_or_path = model_name_or_path
        self._dimension = dimension
        self._max_batch_size = max_batch_size
        self._max_length = max_length
        self._normalize_embeddings = normalize_embeddings
        self._use_fp16 = use_fp16
        self._model = None  # lazy load

    def _load_model(self):
        """延迟加载模型——首次 embed/health 时才初始化，避免 import 时长时间等待。

        加载策略：先离线从本地缓存加载（快速、不联网）；缓存不存在时再联网下载。
        本地路径（目录存在）时直接加载，跳过离线/联网逻辑。
        """
        if self._model is not None:
            return
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError:
            raise ImportError(
                "BGEM3Embedder requires the 'FlagEmbedding' package. "
                "Install it with: pip install FlagEmbedding"
            ) from None

        import os

        is_local_path = os.path.isdir(self._model_name_or_path)

        if not is_local_path:
            # 先尝试离线加载（HF_HUB_OFFLINE=1 → 只读缓存，不联网，快速失败）
            logger.info(
                "BGEM3Embedder: trying offline load from cache for %s", self._model_name_or_path
            )
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                self._model = BGEM3FlagModel(
                    self._model_name_or_path,
                    use_fp16=self._use_fp16,
                )
                logger.info("BGEM3Embedder: model loaded from local cache successfully")
                os.environ.pop("HF_HUB_OFFLINE", None)
                return
            except Exception:
                # 缓存不存在，清理 offline 标记，回退到联网下载
                os.environ.pop("HF_HUB_OFFLINE", None)
                logger.info("BGEM3Embedder: offline load failed, falling back to online download")

        logger.info(
            "BGEM3Embedder: loading model %s (fp16=%s, local=%s)",
            self._model_name_or_path,
            self._use_fp16,
            is_local_path,
        )
        try:
            self._model = BGEM3FlagModel(
                self._model_name_or_path,
                use_fp16=self._use_fp16,
            )
            logger.info("BGEM3Embedder: model loaded successfully")
        except Exception as exc:
            from common.errors import BackendError

            raise BackendError(
                f"BGEM3Embedder: failed to load model {self._model_name_or_path}: {exc}. "
                f"Ensure the model files are in the HuggingFace cache or provide "
                f"a local directory path via config.embedder_bge_m3_model."
            ) from exc

    def plugin_type(self) -> PluginType:
        return PluginType.EMBEDDER

    def health(self) -> None:
        self._load_model()
        try:
            vectors = self._embed_batch(["health check"])
            if len(vectors[0]) != self._dimension:
                raise HealthCheckError(
                    f"BGE-M3 dimension mismatch: expected {self._dimension}, got {len(vectors[0])}"
                )
        except Exception as exc:
            raise HealthCheckError(f"BGEM3Embedder health check failed: {exc}") from exc

    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        self._load_model()
        try:
            all_vectors: List[List[float]] = []
            for batch in self._split_batches(texts):
                vectors = self._embed_batch(batch)
                all_vectors.extend(vectors)
            return all_vectors
        except Exception as exc:
            # 模型已加载成功但 encode 失败——不置 _load_failed，允许后续重试
            logger.error("BGEM3Embedder: encode failed: %s", exc)
            raise

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        # normalize_embeddings 不传给底层 tokenizer（避免版本兼容问题），
        # 而在 encode 之后手动做 L2 归一化。
        result = self._model.encode(
            texts,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
            max_length=self._max_length,
            batch_size=self._max_batch_size,
        )
        dense = result["dense_vecs"]
        # dense 是 numpy ndarray (batch, dim) → 转为 list[list[float]]
        vectors = [row.tolist() for row in dense]
        # 维度截断：如果模型输出维度 > config 设定维度，截断尾部
        if len(vectors) > 0 and len(vectors[0]) > self._dimension:
            dimension = self._dimension
            vectors = [v[:dimension] for v in vectors]
        # 手动 L2 归一化（替代传 normalize_embeddings 参数，兼容所有版本）
        if self._normalize_embeddings:
            vectors = [_l2_normalize(v) for v in vectors]
        return vectors

    def _split_batches(self, texts: List[str]) -> List[List[str]]:
        batches = []
        for i in range(0, len(texts), self._max_batch_size):
            batch_end = i + self._max_batch_size
            batches.append(texts[i:batch_end])
        return batches


# -- 注册到 EmbedderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@EmbedderProducer.register("bge_m3")
def _build(config):
    """Builder: 从配置树节点创建 BGEM3Embedder（参数沿父链回退）。"""
    dim = config.get("embedder_dim", 64)
    return BGEM3Embedder(
        model_name_or_path=config.get("embedder_bge_m3_model", "BAAI/bge-m3"),
        use_fp16=config.get("embedder_bge_m3_fp16", True),
        dimension=dim if dim != 64 else 1024,
        max_batch_size=config.get("embedder_bge_m3_batch_size", 12),
        max_length=config.get("embedder_bge_m3_max_length", 8192),
        normalize_embeddings=config.get("embedder_bge_m3_normalize", True),
    )
