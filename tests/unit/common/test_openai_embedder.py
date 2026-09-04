"""OpenAIEmbedder 单元测试（10 个测试）。

使用 Mock 替换 openai client，隔离外部 API 依赖。
"""

from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.embedder.embedder_impl import EmbedderProducer
from jiuwen_memory.common.embedder.embedder_impl.openai_embedder import OpenAIEmbedder
from jiuwen_memory.common.errors import HealthCheckError

# ---------------------------------------------------------------------------
# Helper: Mock openai client
# ---------------------------------------------------------------------------


class MockEmbeddingResponse:
    """Mock embeddings.create 返回对象。"""

    def __init__(self, texts: list[str], dim: int = 8) -> None:
        self.data = []
        for i, text in enumerate(texts):
            vector = _hash_vector(text, dim)
            self.data.append(MockEmbeddingData(index=i, embedding=vector))


class MockEmbeddingData:
    """Mock 单条 embedding 数据。"""

    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


def _hash_vector(text: str, dim: int) -> list[float]:
    """确定性 hash → 向量（同文本产出相同向量）。"""
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


def _make_mock_embedder(dim: int = 8, max_batch_size: int = 2048) -> OpenAIEmbedder:
    """创建使用 Mock client 的 OpenAIEmbedder。"""
    embedder = OpenAIEmbedder(
        model_name="text-embedding-3-small",
        api_key="mock-key",
        dimension=dim,
        max_batch_size=max_batch_size,
    )
    mock_create = MagicMock(
        side_effect=lambda **kwargs: MockEmbeddingResponse(kwargs["input"], dim=dim)
    )
    mock_client = MagicMock()
    mock_client.embeddings.create = mock_create
    # 惰性 client 无公共注入入口；用 setattr 字符串写入（G.CLS.11 / protected-access）。
    setattr(embedder, "_client", mock_client)
    setattr(embedder, "_client_fingerprint", ("mock-key", None, False, None))
    return embedder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plugin_type():
    """T-EM-01: plugin_type 返回 EMBEDDER。"""
    embedder = _make_mock_embedder()
    assert embedder.plugin_type() == PluginType.EMBEDDER


def test_dimension():
    """T-EM-02: dimension 返回配置的维度。"""
    embedder = _make_mock_embedder(dim=256)
    assert embedder.dimension() == 256


def test_embed_single():
    """T-EM-03: 单条文本向量化。"""
    embedder = _make_mock_embedder(dim=8)
    vectors = embedder.embed(["hello world"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 8
    vectors2 = embedder.embed(["hello world"])
    assert vectors[0] == vectors2[0]


def test_embed_batch():
    """T-EM-04: 批量文本向量化，顺序与输入一致。"""
    texts = ["hello", "world", "test"]
    embedder = _make_mock_embedder(dim=8)
    vectors = embedder.embed(texts)
    assert len(vectors) == 3
    assert len(vectors[0]) == 8
    single = embedder.embed(["hello"])
    assert vectors[0] == single[0]


def test_embed_empty():
    """T-EM-05: 空 list 输入返回空 list。"""
    embedder = _make_mock_embedder()
    result = embedder.embed([])
    assert result == []


def test_embed_auto_split():
    """T-EM-06: 超过 max_batch_size 时自动拆分批次。"""
    embedder = _make_mock_embedder(dim=4, max_batch_size=3)
    texts = ["a", "b", "c", "d", "e", "f", "g"]
    vectors = embedder.embed(texts)
    assert len(vectors) == 7
    assert getattr(embedder, "_client").embeddings.create.call_count == 3


def test_embed_query():
    """T-EM-07: embed_query 单条便捷方法。"""
    embedder = _make_mock_embedder(dim=8)
    vector = embedder.embed_query("test query")
    assert len(vector) == 8
    assert isinstance(vector, list)


def test_health():
    """T-EM-08: health 正常时返回 None。"""
    embedder = _make_mock_embedder()
    assert embedder.health() is None


def test_health_failure():
    """T-EM-09: health 失败时抛 HealthCheckError。"""
    embedder = _make_mock_embedder()
    getattr(embedder, "_client").embeddings.create = MagicMock(side_effect=Exception("API down"))
    with pytest.raises(HealthCheckError):
        embedder.health()


# ---------------------------------------------------------------------------
# Producer tests
# ---------------------------------------------------------------------------


def test_producer_known():
    """T-P-01: EmbedderProducer 已注册 openai 和 hashing。"""
    assert "openai" in EmbedderProducer.known()
    assert "hashing" in EmbedderProducer.known()


def test_producer_unknown_type():
    """T-P-02: 不支持的 embedder_type 抛 ValidationError。"""
    from jiuwen_memory.common.errors import ValidationError
    from jiuwen_memory.config import AssemblyContext

    with pytest.raises(ValidationError):
        EmbedderProducer.build("unknown", {}, AssemblyContext())
