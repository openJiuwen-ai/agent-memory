"""BGEM3Embedder 单元测试。

使用 Mock 替换 FlagEmbedding 模型，隔离外部模型加载依赖。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.base import PluginType
from common.embedder.embedder_impl import EmbedderProducer
from common.embedder.embedder_impl.bge_m3_embedder import BGEM3Embedder
from common.errors import HealthCheckError

# ---------------------------------------------------------------------------
# Helper: Mock BGEM3FlagModel
# ---------------------------------------------------------------------------


def _hash_vector(text: str, dim: int) -> list[float]:
    """确定性 hash → L2 归一化向量（同文本产出相同向量）。"""
    import hashlib
    import math

    h = hashlib.sha256(text.encode()).digest()
    vec = []
    for i in range(dim):
        b = h[i % len(h)]
        vec.append(float(b) / 255.0 - 0.5)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class MockBGEM3Model:
    """Mock FlagEmbedding.BGEM3FlagModel。"""

    def __init__(self, model_name_or_path: str = "", use_fp16: bool = True) -> None:
        self._model_name = model_name_or_path
        self._call_count = 0

    def encode(self, sentences, **_kwargs):
        self._call_count += 1
        dim = 1024
        import numpy as np

        vectors = np.array([_hash_vector(s, dim) for s in sentences])
        return {"dense_vecs": vectors}


def _make_mock_embedder(
    dimension: int = 1024,
    max_batch_size: int = 12,
) -> BGEM3Embedder:
    """创建使用 Mock 模型的 BGEM3Embedder。"""
    embedder = BGEM3Embedder(
        model_name_or_path="BAAI/bge-m3",
        use_fp16=True,
        dimension=dimension,
        max_batch_size=max_batch_size,
    )
    setattr(embedder, "_model", MockBGEM3Model())
    return embedder


# ---------------------------------------------------------------------------
# Tests: Core interface
# ---------------------------------------------------------------------------


def test_plugin_type():
    """T-BM3-01: plugin_type 返回 EMBEDDER。"""
    embedder = _make_mock_embedder()
    assert embedder.plugin_type() == PluginType.EMBEDDER


def test_dimension():
    """T-BM3-02: dimension 返回配置的维度（默认 1024）。"""
    embedder = _make_mock_embedder()
    assert embedder.dimension() == 1024


def test_dimension_custom():
    """T-BM3-03: dimension 支持截断维度（< 1024）。"""
    embedder = _make_mock_embedder(dimension=512)
    assert embedder.dimension() == 512


def test_embed_single():
    """T-BM3-04: 单条文本向量化。"""
    embedder = _make_mock_embedder()
    vectors = embedder.embed(["你好世界"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
    # 确定性：同输入同输出
    vectors2 = embedder.embed(["你好世界"])
    assert vectors[0] == vectors2[0]


def test_embed_batch():
    """T-BM3-05: 批量文本向量化，顺序与输入一致。"""
    texts = ["hello", "世界", "test multilingual"]
    embedder = _make_mock_embedder()
    vectors = embedder.embed(texts)
    assert len(vectors) == 3
    assert len(vectors[0]) == 1024
    single = embedder.embed(["hello"])
    assert vectors[0] == single[0]


def test_embed_multilingual():
    """T-BM3-06: 多语言文本向量化——中英日韩均可产出向量。"""
    texts = [
        "用户偏好简洁回答",
        "The user prefers concise answers",
        "ユーザーは簡潔な回答を好む",
        "사용자는 간결한 답변을 선호합니다",
    ]
    embedder = _make_mock_embedder()
    vectors = embedder.embed(texts)
    assert len(vectors) == 4
    for v in vectors:
        assert len(v) == 1024
        # L2 归一化后向量范数 ≈ 1.0
        import math

        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 0.01


def test_embed_empty():
    """T-BM3-07: 空 list 输入返回空 list。"""
    embedder = _make_mock_embedder()
    result = embedder.embed([])
    assert result == []


def test_embed_auto_split():
    """T-BM3-08: 超过 max_batch_size 时自动拆分批次。"""
    embedder = _make_mock_embedder(max_batch_size=3)
    texts = ["a", "b", "c", "d", "e", "f", "g"]
    vectors = embedder.embed(texts)
    assert len(vectors) == 7
    # Mock 模型 encode 调用次数 = ceil(7/3) = 3
    assert getattr(getattr(embedder, "_model"), "_call_count") == 3


def test_embed_query():
    """T-BM3-09: embed_query 单条便捷方法。"""
    embedder = _make_mock_embedder()
    vector = embedder.embed_query("test query")
    assert len(vector) == 1024
    assert isinstance(vector, list)


def test_health():
    """T-BM3-10: health 正常时返回 None。"""
    embedder = _make_mock_embedder()
    result = embedder.health()
    assert result is None


def test_health_failure():
    """T-BM3-11: health 失败时抛 HealthCheckError。"""
    embedder = BGEM3Embedder()
    # _load_model 失败时 → ImportError（也属于 HealthCheckError 前置）
    # 直接模拟 model.encode 抛异常
    model = MagicMock()
    model.encode = MagicMock(side_effect=RuntimeError("CUDA out of memory"))
    setattr(embedder, "_model", model)
    with pytest.raises(HealthCheckError):
        embedder.health()


def test_lazy_load():
    """T-BM3-12: 模型延迟加载——构造时不加载，首次 embed 时加载。"""
    embedder = BGEM3Embedder()
    assert getattr(embedder, "_model") is None  # 构造时未加载
    # patch _load_model 让它注入 mock
    setattr(embedder, "_model", MockBGEM3Model())  # 模拟 _load_model 成功
    vectors = embedder.embed(["test"])
    assert len(vectors) == 1  # 加载后可正常使用


def test_dimension_truncation():
    """T-BM3-13: 模型输出 1024 维，config 设定 512 → 截断到 512。"""
    embedder = _make_mock_embedder(dimension=512)
    vectors = embedder.embed(["hello"])
    assert len(vectors[0]) == 512


# ---------------------------------------------------------------------------
# Tests: Producer / Config
# ---------------------------------------------------------------------------


def test_producer_known():
    """T-BM3-P01: EmbedderProducer 已注册 bge_m3。"""
    assert "bge_m3" in EmbedderProducer.known()


def test_producer_create():
    """T-BM3-P02: EmbedderProducer.create("bge_m3") 返回 BGEM3Embedder 实例。"""
    from config import AssemblyContext

    embedder = EmbedderProducer.build("bge_m3", {"embedder_dim": 1024}, AssemblyContext())
    assert isinstance(embedder, BGEM3Embedder)
    assert embedder.dimension() == 1024
    assert getattr(embedder, "_model_name_or_path") == "BAAI/bge-m3"


def test_producer_unknown_type():
    """T-BM3-P03: 不支持的 embedder_type 抛 ValidationError。"""
    from common.errors import ValidationError
    from config import AssemblyContext

    with pytest.raises(ValidationError):
        EmbedderProducer.build("unknown", {}, AssemblyContext())
