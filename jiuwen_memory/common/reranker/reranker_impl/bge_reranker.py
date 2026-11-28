# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BGEReranker — BAAI BGE reranker 本地交叉编码精排实现。

使用 ``FlagEmbedding`` 的 ``FlagReranker`` 加载 BGE reranker 模型，对
``query`` 和候选文本组成的 pair 直接计算相关性分。模型延迟加载，只有显式选择
``reranker_backend="bge_reranker"`` 并首次调用时才需要重依赖和模型文件。
"""

from __future__ import annotations

import threading
from typing import Any

from jiuwen_memory.common._support import (
    as_bool,
    resolve_effective_fp16,
    resolve_model_source,
)
from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import BackendError, HealthCheckError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.reranker.base import Reranker, RerankerProducer
from jiuwen_memory.common.type_def import MemoryUnit, Segment

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
        self._model_name_or_path = model_name_or_path
        self._use_fp16 = use_fp16
        self._max_batch_size = max(1, max_batch_size)
        self._normalize = normalize
        self._model = None  # lazy load
        self._model_lock = threading.RLock()

    def plugin_type(self) -> PluginType:
        return PluginType.RERANKER

    def health(self) -> None:
        self._load_model()
        try:
            scores = self.rerank(
                "health check",
                [MemoryUnit(segments=[Segment(content="health check")])],
            )
            if len(scores) != 1:
                raise HealthCheckError(f"BGE reranker returned {len(scores)} scores")
        except HealthCheckError:
            raise
        except Exception as exc:
            raise HealthCheckError(f"BGEReranker health check failed: {exc}") from exc

    def rerank(self, query: str, units: list[MemoryUnit]) -> list[float]:
        if not units:
            return []
        self._load_model()

        scores: list[float] = []
        pairs = [[query, unit.content] for unit in units]
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
        """延迟加载本地模型，避免默认轻量环境在 import 阶段触发重依赖。

        多线程并发首次调用经 ``_model_lock`` 双检串行，保证底层模型只构造一次；
        无 CUDA 时将 ``use_fp16=True`` 强制降级为 fp32，避免 meta tensor 或 CPU
        Half 算子异常。
        """
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            model_source = resolve_model_source(
                self._model_name_or_path, component="BGEReranker"
            )
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as exc:
                raise BackendError(
                    "BGEReranker requires the 'FlagEmbedding' package. "
                    "Install it with: pip install FlagEmbedding"
                ) from exc

            effective_fp16 = resolve_effective_fp16(
                self._use_fp16, component="BGEReranker"
            )
            logger.info(
                "BGEReranker: loading model %s (fp16=%s)",
                model_source,
                effective_fp16,
            )
            try:
                model = FlagReranker(model_source, use_fp16=effective_fp16)
                self._model = model
            except Exception as exc:
                raise BackendError(
                    f"BGEReranker: failed to load model {model_source}: {exc}."
                ) from exc
            logger.info("BGEReranker: model loaded successfully")

    def _compute_score(self, pairs: list[list[str]]) -> list[float]:
        # FlagReranker 单卡推理非线程安全（内部会触发 model.to(device) 等就地状态
        # 变更），按单 batch 串行化底层 compute_score 调用。
        with self._model_lock:
            if self._model is None:
                self._load_model()
            model = self._model
            try:
                raw_scores = model.compute_score(pairs, normalize=self._normalize)
            except TypeError:
                raw_scores = model.compute_score(pairs)
        return _coerce_scores(raw_scores)

    def _split_batches(self, pairs: list[list[str]]) -> list[list[list[str]]]:
        batches = []
        for i in range(0, len(pairs), self._max_batch_size):
            batch_end = i + self._max_batch_size
            batches.append(pairs[i:batch_end])
        return batches


def _coerce_scores(raw_scores: Any) -> list[float]:
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
        use_fp16=as_bool(config.get("reranker_bge_fp16"), default=True),
        max_batch_size=config.get("reranker_bge_batch_size", 16),
        normalize=config.get("reranker_bge_normalize", True),
    )

# F17 D9: data-destination declaration (registered when this module loads).
from jiuwen_memory.common.egress import declare  # noqa: E402

declare('reranker', "bge_reranker", remote=False, note="local reranker model")
