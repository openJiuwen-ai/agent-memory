"""Shared deterministic fixtures for agent-memory tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

import pytest

from jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker
from jiuwen_memory.common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from jiuwen_memory.common.feature_extractor.feature_extractor_impl import keyword_feature_extractor
from jiuwen_memory.common.reranker.reranker_impl.overlap_reranker import OverlapReranker
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import RetrievalPipeline
from jiuwen_memory.common.type_def.memory import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Segment,
    Temporal,
)
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.retriever_impl.unit_reader import UnitReader
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.domain_store_impl.vector_recaller import VectorRecaller
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.graph import GraphStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.vector import VectorStore
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

DEFAULT_SCOPE = Scope(org="acme", user="u1", agent="a1", session="s1")


def make_storage(
    *,
    kv: KVStore | None = None,
    vector: VectorStore | None = None,
    fulltext: InMemoryFulltextStore | None = None,
    graph: GraphStore | None = None,
    security: Any = None,
    pipeline: RetrievalPipeline = RetrievalPipeline.RECALL_GET_RANK,
) -> CompositeStoreManager:
    """CompositeStoreManager + 已绑定 default 数据面（F07 双面结构）。

    端口消费者直接持返回的 manager；数据面消费者经 ``manager.domain_store()``
    取 DomainStore。recallers 不在此装配——需召回的测试自建
    ``CompositeDomainStore`` 后 ``bind_recallers`` + ``bind_domain_store``。
    """
    manager = CompositeStoreManager(
        kv=kv, vector=vector, fulltext=fulltext, graph=graph, security=security
    )
    manager.bind_domain_store(
        CompositeDomainStore(manager=manager, preferred_pipeline=pipeline)
    )
    return manager


@dataclass
class RetrievalWorld:
    """A deterministic retrieval stack assembled from real in-memory components."""

    tokenizer: WhitespaceTokenizer
    embedder: HashingEmbedder
    kv: InMemoryKVStore
    vector: InMemoryVectorStore
    fulltext: InMemoryFulltextStore
    parser: SimpleQueryParser
    keyword: KeywordRecaller
    vector_recaller: VectorRecaller
    discloser: TruncatingDiscloser
    unit_reader: UnitReader
    retriever: PipelineRetriever


def make_world(rerank: bool = False) -> RetrievalWorld:
    tokenizer = WhitespaceTokenizer()
    embedder = HashingEmbedder(tokenizer)
    features = keyword_feature_extractor.KeywordFeatureExtractor(tokenizer)
    kv = InMemoryKVStore()
    vector = InMemoryVectorStore()
    fulltext = InMemoryFulltextStore(tokenizer)
    storage = CompositeStoreManager(kv=kv, vector=vector, fulltext=fulltext)
    domain_store = CompositeDomainStore(
        manager=storage, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK
    )
    keyword = KeywordRecaller(storage)
    vector_recaller = VectorRecaller(storage)
    domain_store.bind_recallers([keyword, vector_recaller])
    storage.bind_domain_store(domain_store)
    parser = SimpleQueryParser(tokenizer, embedder, feature_extractor=features)
    discloser = TruncatingDiscloser()
    unit_reader = UnitReader(kv)
    reranker = OverlapReranker(tokenizer) if rerank else None
    retriever = PipelineRetriever(
        parser,
        RRFFuser(),
        discloser,
        unit_reader,
        reranker,
        domain_store=domain_store,
    )
    return RetrievalWorld(
        tokenizer,
        embedder,
        kv,
        vector,
        fulltext,
        parser,
        keyword,
        vector_recaller,
        discloser,
        unit_reader,
        retriever,
    )


def make_unit(
    uid: str,
    content: str,
    *,
    scope: Scope = DEFAULT_SCOPE,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    t_event: Optional[datetime] = None,
    t_valid: Optional[datetime] = None,
    t_invalid: Optional[datetime] = None,
    t_message: Optional[datetime] = None,
    supersedes: str = "",
    tags: Optional[list[str]] = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(
            t_event=t_event, t_valid=t_valid, t_invalid=t_invalid, t_message=t_message
        ),
        supersedes=supersedes,
        tags=list(tags or []),
        lifecycle=lifecycle,
    )


def index_unit(world: RetrievalWorld, unit: MemoryUnit) -> None:
    """Use the production write-side projection for retrieval tests."""
    storage = CompositeStoreManager(
        kv=world.kv,
        vector=world.vector,
        fulltext=world.fulltext,
    )
    builder = HybridIndexBuilder(
        storage,
        FixedWindowChunker(),
        world.embedder,
        layers_enabled=False,
    )
    builder.build([unit])


@pytest.fixture
def scope() -> Scope:
    return DEFAULT_SCOPE


@pytest.fixture
def world() -> RetrievalWorld:
    return make_world()


@pytest.fixture
def unit_factory() -> Callable[..., MemoryUnit]:
    return make_unit


@pytest.fixture
def index_unit_fn() -> Callable[[RetrievalWorld, MemoryUnit], None]:
    return index_unit
