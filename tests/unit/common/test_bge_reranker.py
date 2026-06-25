"""BGEReranker 单元测试。

使用 Mock 替换 FlagEmbedding reranker，隔离外部模型加载依赖。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from common.base import PluginType
from common.errors import HealthCheckError
from common.reranker.reranker_impl import RerankerProducer
from common.reranker.reranker_impl.bge_reranker import BGEReranker
from config import AssemblyContext


class MockBGEModel:
    """Mock FlagEmbedding.FlagReranker。"""

    def __init__(self) -> None:
        self.calls = []

    def compute_score(self, sentence_pairs, normalize=True):
        self.calls.append((sentence_pairs, normalize))
        scores = []
        for query, text in sentence_pairs:
            query_tokens = set(query.lower().split())
            text_tokens = set(text.lower().split())
            if not query_tokens:
                scores.append(0.0)
            else:
                scores.append(len(query_tokens & text_tokens) / len(query_tokens))
        return scores


class MockFlagReranker(MockBGEModel):
    """Mock FlagReranker 构造器，记录模型加载参数。"""

    def __init__(self, model_name_or_path: str, use_fp16: bool = True) -> None:
        super().__init__()
        self.model_name_or_path = model_name_or_path
        self.use_fp16 = use_fp16


class MockBGEModelWithoutNormalize:
    """模拟旧版 compute_score 不支持 normalize 参数。"""

    @staticmethod
    def compute_score(sentence_pairs):
        return [1.0 for _query, _text in sentence_pairs]


def _make_mock_reranker(
    max_batch_size: int = 16,
    normalize: bool = True,
) -> BGEReranker:
    reranker = BGEReranker(
        model_name_or_path="BAAI/bge-reranker-v2-m3",
        use_fp16=True,
        max_batch_size=max_batch_size,
        normalize=normalize,
    )
    setattr(reranker, "_model", MockBGEModel())
    return reranker


def test_plugin_type():
    """T-BGE-RR-01: plugin_type 返回 RERANKER。"""
    reranker = _make_mock_reranker()
    assert reranker.plugin_type() == PluginType.RERANKER


def test_rerank_empty_does_not_load_model():
    """T-BGE-RR-02: 空候选返回空分数，并且不加载模型。"""
    reranker = BGEReranker()
    assert reranker.rerank("coffee", []) == []
    assert getattr(reranker, "_model") is None


def test_rerank_scores_keep_input_order():
    """T-BGE-RR-03: 批量精排分数顺序与候选文本顺序一致。"""
    reranker = _make_mock_reranker()
    scores = reranker.rerank(
        "coffee morning",
        [
            "alice drinks coffee every morning",
            "bob prefers tea",
            "morning coffee notes",
        ],
    )

    assert scores == [1.0, 0.0, 1.0]


def test_rerank_auto_split_and_passes_normalize_flag():
    """T-BGE-RR-04: 超过 batch size 自动拆分，并透传 normalize 配置。"""
    reranker = _make_mock_reranker(max_batch_size=2, normalize=False)
    scores = reranker.rerank("coffee", ["coffee", "tea", "coffee tea", "water", "coffee"])

    assert scores == [1.0, 0.0, 1.0, 0.0, 1.0]
    model = getattr(reranker, "_model")
    assert len(model.calls) == 3
    assert all(call[1] is False for call in model.calls)


def test_rerank_supports_compute_score_without_normalize_argument():
    """T-BGE-RR-05: 兼容旧版 FlagReranker.compute_score 不支持 normalize 参数。"""
    reranker = BGEReranker()
    setattr(reranker, "_model", MockBGEModelWithoutNormalize())

    assert reranker.rerank("coffee", ["coffee", "tea"]) == [1.0, 1.0]


def test_health_ok():
    """T-BGE-RR-06: health 正常时返回 None。"""
    reranker = _make_mock_reranker()
    assert reranker.health() is None


def test_health_failure():
    """T-BGE-RR-07: health 失败时抛 HealthCheckError。"""
    reranker = BGEReranker()
    model = MagicMock()
    model.compute_score = MagicMock(side_effect=RuntimeError("model unavailable"))
    setattr(reranker, "_model", model)

    with pytest.raises(HealthCheckError):
        reranker.health()


def test_lazy_load_from_flagembedding():
    """T-BGE-RR-08: 首次 rerank 时才从 FlagEmbedding 加载模型。"""
    flag_embedding = SimpleNamespace(FlagReranker=MockFlagReranker)
    with patch.dict(sys.modules, {"FlagEmbedding": flag_embedding}):
        reranker = BGEReranker(
            model_name_or_path="BAAI/bge-reranker-v2-m3",
            use_fp16=False,
        )
        assert getattr(reranker, "_model") is None

        scores = reranker.rerank("coffee", ["coffee note"])

    assert scores == [1.0]
    model = getattr(reranker, "_model")
    assert isinstance(model, MockFlagReranker)
    assert model.model_name_or_path == "BAAI/bge-reranker-v2-m3"
    assert model.use_fp16 is False


def test_missing_flagembedding_dependency_reports_clear_error():
    """T-BGE-RR-09: 缺少 FlagEmbedding 时，选择该后端才抛清晰错误。"""
    with patch.dict(sys.modules, {"FlagEmbedding": None}):
        reranker = BGEReranker()
        with pytest.raises(ImportError, match="FlagEmbedding"):
            reranker.rerank("coffee", ["coffee note"])


def test_producer_known():
    """T-BGE-RR-P01: RerankerProducer 已注册 bge_reranker。"""
    assert "bge_reranker" in RerankerProducer.known()


def test_producer_create_from_config():
    """T-BGE-RR-P02: RerankerProducer.create 可按配置创建 BGEReranker。"""
    reranker = RerankerProducer.build(
        "bge_reranker",
        {
            "reranker_bge_model": "BAAI/bge-reranker-v2-m3",
            "reranker_bge_fp16": False,
            "reranker_bge_batch_size": 4,
            "reranker_bge_normalize": False,
        },
        AssemblyContext(),
    )

    assert isinstance(reranker, BGEReranker)
    assert getattr(reranker, "_model_name_or_path") == "BAAI/bge-reranker-v2-m3"
    assert getattr(reranker, "_use_fp16") is False
    assert getattr(reranker, "_max_batch_size") == 4
    assert getattr(reranker, "_normalize") is False
