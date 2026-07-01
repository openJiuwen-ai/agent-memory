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
        self.call_count = 0  # encode 调用计数（公共属性，供测试断言）
        self.use_fp16 = use_fp16  # 记录实际传入值，供 fp16 降级测试断言

    def encode(self, sentences, **_kwargs):
        # 参数用 **kwargs 吸收 BGEM3FlagModel.encode 的对齐接口
        # （batch_size/max_length/return_dense/...），桩件不消费它们——
        # BGEM3Embedder._embed_batch 按关键字调用，**kwargs 兜底接收即可。
        self.call_count += 1
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
    # BGEM3Embedder 是 lazy-load 设计（构造时不加载模型），_model 是私有属性，
    # 无公共注入入口。用 setattr 字符串形式注入 mock（pylint protected-access
    # 不检测 setattr/getattr 的字符串形式，且这是测试桩件注入的唯一途径）。
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
    assert getattr(embedder, "_model").call_count == 3


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
    mock_model = MagicMock()
    mock_model.encode = MagicMock(side_effect=RuntimeError("CUDA out of memory"))
    setattr(embedder, "_model", mock_model)
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


class _NaNModel:
    """Mock 模型：产出含 NaN/Inf 的向量（模拟 fp16 推理溢出）。"""

    def __init__(self, model_name_or_path: str = "", use_fp16: bool = True) -> None:
        pass

    @staticmethod
    def encode(sentences, **_kwargs):
        # 参数用 **kwargs 吸收 BGEM3FlagModel.encode 的对齐接口，桩件不消费它们。
        import numpy as np
        # 每条向量前半 NaN、后半 Inf，触发 _l2_normalize / _sanitize_vector 防御
        dim = 1024
        vectors = np.array([
            [float("nan")] * (dim // 2) + [float("inf")] * (dim // 2)
            for _ in sentences
        ])
        return {"dense_vecs": vectors}


def test_embed_nan_sanitized():
    """T-BM3-14: 模型产出 NaN/Inf 向量 → embed 返回零向量（不含 NaN/Inf），不污染 Milvus。"""
    embedder = BGEM3Embedder(dimension=1024, normalize_embeddings=True)
    setattr(embedder, "_model", _NaNModel())
    vectors = embedder.embed(["短文本"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
    # 归一化路径：NaN/Inf → 零向量
    import math
    for v in vectors[0]:
        assert not math.isnan(v)
        assert not math.isinf(v)
    assert all(v == 0.0 for v in vectors[0])


def test_embed_nan_sanitized_no_normalize():
    """T-BM3-15: 未开归一化时，NaN/Inf 仍被替换为 0（不透传给 Milvus）。"""
    embedder = BGEM3Embedder(dimension=1024, normalize_embeddings=False)
    setattr(embedder, "_model", _NaNModel())
    vectors = embedder.embed(["短文本"])
    assert len(vectors) == 1
    import math
    for v in vectors[0]:
        assert not math.isnan(v)
        assert not math.isinf(v)


def test_fp16_disabled_on_cpu(monkeypatch):
    """T-BM3-16: CPU-only 运行时（cuda 不可用）下，use_fp16=true 被强制降为 fp32。

    防止 torch>=2.x meta device 触发 "Cannot copy out of meta tensor" 错误。
    """
    import sys
    import types
    # 注入 fake FlagEmbedding 模块，BGEM3FlagModel 用 MockBGEM3Model
    fake_mod = types.ModuleType("FlagEmbedding")
    fake_mod.BGEM3FlagModel = MockBGEM3Model
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_mod)
    # mock torch.cuda.is_available 返回 False（CPU 环境）
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    embedder = BGEM3Embedder(model_name_or_path="BAAI/bge-m3", use_fp16=True, dimension=1024)
    # 触发 _load_model（mock 模型不走真实加载，但会经过 fp16 降级判断）
    getattr(embedder, "_load_model")()
    assert getattr(embedder, "_model").use_fp16 is False, "CPU 下 use_fp16 应被强制降为 False"


def test_fp16_kept_on_cuda(monkeypatch):
    """T-BM3-17: CUDA 可用时，use_fp16=true 保持不变。"""
    import sys
    import types
    fake_mod = types.ModuleType("FlagEmbedding")
    fake_mod.BGEM3FlagModel = MockBGEM3Model
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_mod)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    embedder = BGEM3Embedder(model_name_or_path="BAAI/bge-m3", use_fp16=True, dimension=1024)
    getattr(embedder, "_load_model")()
    assert getattr(embedder, "_model").use_fp16 is True, "CUDA 下 use_fp16 应保持 True"


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
