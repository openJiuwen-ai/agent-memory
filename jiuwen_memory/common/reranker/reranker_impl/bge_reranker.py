"""BGEReranker — BAAI BGE reranker 本地交叉编码精排实现。

使用 ``FlagEmbedding`` 的 ``FlagReranker`` 加载 BGE reranker 模型，对
``query`` 和候选文本组成的 pair 直接计算相关性分。模型延迟加载，只有显式选择
``reranker_backend="bge_reranker"`` 并首次调用时才需要重依赖和模型文件。
"""

from __future__ import annotations

from typing import Any, List

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import HealthCheckError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer

logger = get_logger(__name__)


class BGEReranker(Reranker):
    """基于 ``BAAI/bge-reranker-v2-m3`` 的本地 cross-encoder 精排器。"""

    def __init__(
        self,
        model_name_or_path: str = "BAAI/bge-reranker-v2-m3",
        use_fp16: bool = True,
        max_batch_size: int = 16,
        normalize: bool = True,
    ) -> None:
        """初始化 BGEReranker。

        Args:
            model_name_or_path: 参数 model_name_or_path（str）。
            use_fp16: 参数 use_fp16（bool）。
            max_batch_size: 参数 max_batch_size（int）。
            normalize: 参数 normalize（bool）。
        """
        self._model_name_or_path = model_name_or_path
        self._use_fp16 = use_fp16
        self._max_batch_size = max(1, max_batch_size)
        self._normalize = normalize
        self._model = None  # lazy load

    def plugin_type(self) -> PluginType:
        """返回当前插件类型。

        Returns:
            返回 PluginType。
        """
        return PluginType.RERANKER

    def health(self) -> None:
        """执行健康检查。

        Raises:
            HealthCheckError: 执行失败时抛出。
        """
        self._load_model()
        try:
            scores = self.rerank("health check", ["health check"])
            if len(scores) != 1:
                raise HealthCheckError(f"BGE reranker returned {len(scores)} scores")
        except HealthCheckError:
            raise
        except Exception as exc:
            raise HealthCheckError(f"BGEReranker health check failed: {exc}") from exc

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        """对候选结果重新排序并评分。

        Args:
            query: 参数 query（str）。
            texts: 参数 texts（List[str]）。

        Returns:
            返回 List[float]。

        Raises:
            ValueError: 执行失败时抛出。
        """
        if not texts:
            return []
        self._load_model()

        scores: List[float] = []
        pairs = [[query, text] for text in texts]
        for batch in self._split_batches(pairs):
            batch_scores = self._compute_score(batch)
            if len(batch_scores) != len(batch):
                raise ValueError(
                    "BGE reranker score count mismatch: "
                    f"expected {len(batch)}, got {len(batch_scores)}"
                )
            scores.extend(batch_scores)
        return scores

    def _load_model(self) -> None:
        """延迟加载模型，避免默认轻量环境在 import 阶段触发重依赖。"""
        if self._model is not None:
            return
        try:
            from FlagEmbedding import FlagReranker
        except ImportError:
            raise ImportError(
                "BGEReranker requires the 'FlagEmbedding' package. "
                "Install it with: pip install FlagEmbedding"
            ) from None

        logger.info(
            "BGEReranker: loading model %s (fp16=%s)",
            self._model_name_or_path,
            self._use_fp16,
        )
        self._model = FlagReranker(self._model_name_or_path, use_fp16=self._use_fp16)
        logger.info("BGEReranker: model loaded successfully")

    def _compute_score(self, pairs: List[List[str]]) -> List[float]:
        """执行 `compute_score` 操作。

        Args:
            pairs: 参数 pairs（List[List[str]]）。

        Returns:
            返回 List[float]。
        """
        try:
            raw_scores = self._model.compute_score(pairs, normalize=self._normalize)
        except TypeError:
            raw_scores = self._model.compute_score(pairs)
        return _coerce_scores(raw_scores)

    def _split_batches(self, pairs: List[List[str]]) -> List[List[List[str]]]:
        """执行 `split_batches` 操作。

        Args:
            pairs: 参数 pairs（List[List[str]]）。

        Returns:
            返回 List[List[List[str]]]。
        """
        batches = []
        for i in range(0, len(pairs), self._max_batch_size):
            batch_end = i + self._max_batch_size
            batches.append(pairs[i:batch_end])
        return batches


def _coerce_scores(raw_scores: Any) -> List[float]:
    """执行 `coerce_scores` 操作。

    Args:
        raw_scores: 参数 raw_scores（Any）。

    Returns:
        返回 List[float]。
    """
    if hasattr(raw_scores, "tolist"):
        raw_scores = raw_scores.tolist()
    if isinstance(raw_scores, (int, float)):
        return [float(raw_scores)]
    return [float(score) for score in raw_scores]


# -- 注册到 RerankerProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@RerankerProducer.register("bge_reranker")
def _build(config):
    """Builder: 从配置树节点创建 BGEReranker（参数沿父链回退）。"""
    return BGEReranker(
        model_name_or_path=config.get("reranker_bge_model", "BAAI/bge-reranker-v2-m3"),
        use_fp16=config.get("reranker_bge_fp16", True),
        max_batch_size=config.get("reranker_bge_batch_size", 16),
        normalize=config.get("reranker_bge_normalize", True),
    )
