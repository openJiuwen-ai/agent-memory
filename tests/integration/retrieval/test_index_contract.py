"""检索层 × 构建层 索引契约测试：走真实 HybridIndexBuilder（chunk 粒度建索引），
验证召回统一回传 ``unit.id``（而非 chunk 复合 id）。

锁住的契约：构建层向量索引按 chunk 建、记录 id 为 chunk 复合 id，但在
``metadata['unit_id']`` 写了所属 unit；检索层据此把命中归并回 unit 粒度（MaxP）。
区别于 conftest 的 ``id=unit.id`` 直插夹具——本测试专门覆盖「真实 chunk-id 索引」路径，
防止向量通道因 id 粒度不一致而召回结果被 UnitReader 丢弃的回归。
"""

from __future__ import annotations

import pytest

from common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker
from common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
    KeywordFeatureExtractor,
)
from common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from common.type_def import memory_key
from common.type_def.memory_codec import dumps
from construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from retrieval.recaller_impl.keyword_recaller import KeywordRecaller
from retrieval.recaller_impl.vector_recaller import VectorRecaller
from retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from retrieval.retriever_impl.unit_reader import UnitReader
from retrieval.types import RecallChannel, RetrievalQuery
from storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.storage_impl.composite_storage import CompositeStorage
from storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from tests.conftest import make_unit

pytestmark = pytest.mark.integration


@pytest.fixture
def indexed_via_builder():
    """用真实 HybridIndexBuilder 建索引（chunk 粒度）+ 正排 KV，组装检索栈。"""
    tokenizer = WhitespaceTokenizer()
    embedder = HashingEmbedder(tokenizer)
    features = KeywordFeatureExtractor(tokenizer)
    kv = InMemoryKVStore()
    vector = InMemoryVectorStore()
    fulltext = InMemoryFulltextStore(tokenizer)
    # size 调小，强制把内容切成多个 chunk，覆盖「同 unit 多 chunk → MaxP 折叠」
    chunker = FixedWindowChunker(size=20)
    storage = CompositeStorage(kv=kv, vector=vector, fulltext=fulltext)
    index_builder = HybridIndexBuilder(storage, chunker, embedder)

    parser = SimpleQueryParser(tokenizer, embedder, feature_extractor=features)
    retriever = PipelineRetriever(
        parser,
        [KeywordRecaller(storage), VectorRecaller(storage)],
        RRFFuser(),
        TruncatingDiscloser(),
        UnitReader(kv),
        storage=storage,
    )

    unit = make_unit("u_long", "alice loves iced americano coffee every single morning before work")
    kv.insert(unit.scope, memory_key(unit.id), dumps(unit))  # 正排真源（控制层写链路的等价物）
    index_builder.build([unit])  # 派生索引：向量按 chunk、全文按 unit
    return retriever, unit


def test_vector_channel_resolves_chunk_ids_to_unit_id(indexed_via_builder) -> None:
    """隔离向量通道：chunk 复合 id 必须归并回 unit.id，否则该候选会被 UnitReader 丢弃。"""
    retriever, unit = indexed_via_builder

    result = retriever.retrieve(
        unit.scope,
        RetrievalQuery(text="coffee", top_k=5, channels=[RecallChannel.VECTOR]),
    )

    ids = [item.unit_id for item in result.items]
    assert unit.id in ids  # 命中并解析到 unit
    assert all("-" not in i or i == unit.id for i in ids)  # 不再回传 chunk 复合 id


def test_hybrid_recall_merges_both_channels(indexed_via_builder) -> None:
    """全通道：keyword(unit 粒度) 与 vector(chunk 粒度) 统一到同一 unit.id 后被 RRF 合并。"""
    retriever, unit = indexed_via_builder

    result = retriever.retrieve(
        unit.scope,
        RetrievalQuery(text="coffee morning", top_k=5, with_trajectory=True),
    )

    assert [item.unit_id for item in result.items].count(unit.id) == 1  # 不重复
    assert unit.id in [item.unit_id for item in result.items]
