"""BGEReranker 单元测试。

使用 Mock 替换 FlagEmbedding reranker，隔离外部模型加载依赖。
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import BackendError, HealthCheckError
from jiuwen_memory.common.reranker.reranker_impl import RerankerProducer
from jiuwen_memory.common.reranker.reranker_impl.bge_reranker import BGEReranker
from jiuwen_memory.config import AssemblyContext


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
    scores = reranker.rerank_texts(
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
    scores = reranker.rerank_texts("coffee", ["coffee", "tea", "coffee tea", "water", "coffee"])

    assert scores == [1.0, 0.0, 1.0, 0.0, 1.0]
    model = getattr(reranker, "_model")
    assert len(model.calls) == 3
    assert all(call[1] is False for call in model.calls)


def test_rerank_supports_compute_score_without_normalize_argument():
    """T-BGE-RR-05: 兼容旧版 FlagReranker.compute_score 不支持 normalize 参数。"""
    reranker = BGEReranker()
    setattr(reranker, "_model", MockBGEModelWithoutNormalize())

    assert reranker.rerank_texts("coffee", ["coffee", "tea"]) == [1.0, 1.0]


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


def test_lazy_load_from_flagembedding(tmp_path, monkeypatch):
    """T-BGE-RR-08: 首次 rerank 时才从 FlagEmbedding 加载模型。"""
    flag_embedding = SimpleNamespace(FlagReranker=MockFlagReranker)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    with patch.dict(sys.modules, {"FlagEmbedding": flag_embedding}):
        reranker = BGEReranker(
            model_name_or_path="BAAI/bge-reranker-v2-m3",
            use_fp16=False,
        )
        assert getattr(reranker, "_model") is None

        scores = reranker.rerank_texts("coffee", ["coffee note"])

    assert scores == [1.0]
    model = getattr(reranker, "_model")
    assert isinstance(model, MockFlagReranker)
    assert model.model_name_or_path == str(tmp_path)
    assert model.use_fp16 is False


def test_missing_flagembedding_dependency_reports_backend_error(tmp_path):
    """本地模型存在但依赖缺失时，快速失败且不提前触发 torch CUDA 检测。"""
    cuda_probe = MagicMock(return_value=False)
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=cuda_probe))
    with patch.dict(sys.modules, {"FlagEmbedding": None, "torch": fake_torch}):
        reranker = BGEReranker(model_name_or_path=str(tmp_path), use_fp16=True)
        with pytest.raises(BackendError, match="FlagEmbedding"):
            reranker.rerank_texts("coffee", ["coffee note"])
    cuda_probe.assert_not_called()


def test_fp16_disabled_on_cpu(tmp_path, monkeypatch):
    """T-BGE-RR-09: CPU-only 运行时（cuda 不可用）下，use_fp16=true 被强制降为 fp32。"""
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    fake_flag = SimpleNamespace(FlagReranker=MockFlagReranker)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    reranker = BGEReranker(model_name_or_path="BAAI/bge-reranker-v2-m3", use_fp16=True)
    getattr(reranker, "_load_model")()
    assert getattr(reranker, "_model").use_fp16 is False, "CPU 下 use_fp16 应被强制降为 False"


def test_fp16_kept_on_cuda(tmp_path, monkeypatch):
    """T-BGE-RR-10: CUDA 可用时，use_fp16=true 保持不变。"""
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    fake_flag = SimpleNamespace(FlagReranker=MockFlagReranker)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    reranker = BGEReranker(model_name_or_path="BAAI/bge-reranker-v2-m3", use_fp16=True)
    getattr(reranker, "_load_model")()
    assert getattr(reranker, "_model").use_fp16 is True, "CUDA 下 use_fp16 应保持 True"


def test_fp16_disabled_when_torch_unavailable(tmp_path, monkeypatch):
    """T-BGE-RR-11: torch 不可导入时，use_fp16=true 安全降级为 False。"""
    fake_flag = SimpleNamespace(FlagReranker=MockFlagReranker)
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    reranker = BGEReranker(model_name_or_path="BAAI/bge-reranker-v2-m3", use_fp16=True)
    getattr(reranker, "_load_model")()
    assert getattr(reranker, "_model").use_fp16 is False, "torch 缺失时应降级为 fp32"


class _ConcurrencyDetectingFlagReranker:
    """模拟非线程安全的 FlagReranker：并发进入 compute_score 时抛出 meta tensor 错误。"""

    def __init__(self, model_name_or_path: str = "", use_fp16: bool = True) -> None:
        self.model_name_or_path = model_name_or_path
        self.use_fp16 = use_fp16
        self._guard = threading.Lock()
        self._active = 0
        self.max_concurrent = 0
        self.call_count = 0

    def compute_score(self, sentence_pairs, normalize=True):
        _ = normalize
        with self._guard:
            self._active += 1
            self.call_count += 1
            if self._active > self.max_concurrent:
                self.max_concurrent = self._active
            overlapping = self._active > 1
        try:
            time.sleep(0.01)
            if overlapping:
                raise NotImplementedError("Cannot copy out of meta tensor; no data!")
            return [
                1.0 if query == text else 0.25
                for query, text in sentence_pairs
            ]
        finally:
            with self._guard:
                self._active -= 1


def test_concurrent_rerank_serializes_model_and_preserves_scores():
    """T-BGE-RR-12: 多线程并发 rerank 时底层模型串行执行，且每线程分数完整无串位。"""
    reranker = BGEReranker(max_batch_size=2)
    model = _ConcurrencyDetectingFlagReranker()
    setattr(reranker, "_model", model)

    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def _run(idx: int) -> list[float]:
        query = f"q-{idx}"
        candidates = [f"q-{idx}", f"other-{idx}", f"q-{idx}"]
        barrier.wait()
        return reranker.rerank_texts(query, candidates)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(_run, range(worker_count)))

    assert model.max_concurrent == 1, f"expected max_concurrent=1, got {model.max_concurrent}"
    assert model.call_count == worker_count * 2, "each 3-item input with batch_size=2 takes 2 calls"
    for idx, scores in enumerate(results):
        assert scores == [1.0, 0.25, 1.0], f"worker {idx} got unexpected scores: {scores}"


def test_concurrent_lazy_load_initializes_once(tmp_path, monkeypatch):
    """T-BGE-RR-13: 多线程并发首次调用时，懒加载仅构造一次底层模型。"""
    init_guard = threading.Lock()
    init_count = 0
    created_models: list[_ConcurrencyDetectingFlagReranker] = []

    def _build_model(model_name_or_path: str, use_fp16: bool = True):
        nonlocal init_count
        time.sleep(0.03)
        with init_guard:
            init_count += 1
        instance = _ConcurrencyDetectingFlagReranker(model_name_or_path, use_fp16=use_fp16)
        created_models.append(instance)
        return instance

    fake_flag = SimpleNamespace(FlagReranker=_build_model)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    reranker = BGEReranker(model_name_or_path="BAAI/bge-reranker-v2-m3", use_fp16=False)
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def _run(idx: int) -> list[float]:
        barrier.wait()
        return reranker.rerank_texts(f"k-{idx}", [f"k-{idx}", "miss"])

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(_run, range(worker_count)))

    assert init_count == 1, f"lazy load should run once, got {init_count}"
    assert len(created_models) == 1, "only one reranker model should be created"
    assert created_models[0].max_concurrent == 1, "concurrent compute_score should be serialized"
    for idx, scores in enumerate(results):
        assert scores == [1.0, 0.25], f"worker {idx} scores mismatch: {scores}"


def test_concurrent_lazy_load_failure_allows_retry(tmp_path, monkeypatch):
    """T-BGE-RR-14: 并发首次加载失败后释放锁且不残留半成品，等待线程接力重试成功。"""
    attempts = 0
    attempt_guard = threading.Lock()
    created_models: list[_ConcurrencyDetectingFlagReranker] = []

    def _flaky_model(model_name_or_path: str, use_fp16: bool = True):
        nonlocal attempts
        with attempt_guard:
            attempts += 1
            current = attempts
        if current == 1:
            time.sleep(0.03)
            raise RuntimeError("transient reranker load failure")
        instance = _ConcurrencyDetectingFlagReranker(model_name_or_path, use_fp16=use_fp16)
        created_models.append(instance)
        return instance

    fake_flag = SimpleNamespace(FlagReranker=_flaky_model)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setattr(
        "jiuwen_memory.common.reranker.reranker_impl.bge_reranker.resolve_model_source",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    reranker = BGEReranker(model_name_or_path="BAAI/bge-reranker-v2-m3", use_fp16=False)
    worker_count = 4
    barrier = threading.Barrier(worker_count)

    def _run(idx: int) -> tuple[int, list[float] | None, Exception | None]:
        barrier.wait()
        try:
            return idx, reranker.rerank_texts(f"q-{idx}", [f"q-{idx}", "miss"]), None
        except BackendError as exc:
            return idx, None, exc

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        outcomes = list(pool.map(_run, range(worker_count)))

    errors = [exc for _, _, exc in outcomes if exc is not None]
    successes = [scores for _, scores, exc in outcomes if exc is None and scores is not None]

    assert len(errors) == 1, f"expected 1 BackendError from initial failure, got {errors}"
    assert "transient reranker load failure" in str(errors[0])
    assert len(successes) == worker_count - 1, "waiting threads should recover and succeed"
    assert attempts == 2, f"expected 1 failed + 1 successful load, got {attempts}"
    assert len(created_models) == 1, "only one reranker model should be published after recovery"
    assert getattr(reranker, "_model") is created_models[0]
    assert created_models[0].max_concurrent == 1, "post-recovery compute_score must stay serialized"
    for scores in successes:
        assert scores == [1.0, 0.25], f"unexpected recovered scores: {scores}"


def test_concurrent_health_and_rerank_no_deadlock():
    """T-BGE-RR-15: health 与 rerank 并发交错调用无死锁且保持底层互斥。"""
    reranker = BGEReranker()
    model = _ConcurrencyDetectingFlagReranker()
    setattr(reranker, "_model", model)

    worker_count = 6
    barrier = threading.Barrier(worker_count)

    def _run(idx: int) -> None:
        barrier.wait()
        if idx % 2 == 0:
            reranker.health()
        else:
            scores = reranker.rerank_texts(f"q-{idx}", [f"q-{idx}"])
            assert scores == [1.0], f"unexpected scores for worker {idx}: {scores}"

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(_run, range(worker_count)))

    assert model.max_concurrent == 1, f"expected max_concurrent=1, got {model.max_concurrent}"


def test_producer_known():
    """T-BGE-RR-P01: RerankerProducer 已注册 bge_reranker。"""
    assert "bge_reranker" in RerankerProducer.known()


def test_producer_create_from_config():
    """T-BGE-RR-P02: RerankerProducer.create 可按配置创建 BGEReranker。"""
    reranker = RerankerProducer.build(
        "bge_reranker",
        {
            "reranker_bge_model": "BAAI/bge-reranker-v2-m3",
            "reranker_bge_fp16": "false",
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
