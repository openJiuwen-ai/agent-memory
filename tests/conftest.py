"""Shared deterministic fixtures for agent-memory tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import pytest

from common.embedder.embedder_impl.hashing_embedder import HashingEmbedder
from common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
    KeywordFeatureExtractor,
)
from common.reranker.reranker_impl.overlap_reranker import OverlapReranker
from common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from common.type_def import memory_key
from common.type_def.memory import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Segment,
    Temporal,
)
from common.type_def.memory_codec import dumps
from common.type_def.scope import Scope
from retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from retrieval.recaller_impl.keyword_recaller import KeywordRecaller
from retrieval.recaller_impl.vector_recaller import VectorRecaller
from retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from retrieval.retriever_impl.unit_reader import UnitReader
from storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.storage_impl.composite_storage import CompositeStorage
from storage.types import Document, VectorRecord
from storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

DEFAULT_SCOPE = Scope(org="acme", user="u1", agent="a1", session="s1")


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
    features = KeywordFeatureExtractor(tokenizer)
    kv = InMemoryKVStore()
    vector = InMemoryVectorStore()
    fulltext = InMemoryFulltextStore(tokenizer)
    storage = CompositeStorage(kv=kv, vector=vector, fulltext=fulltext)
    parser = SimpleQueryParser(tokenizer, embedder, feature_extractor=features)
    keyword = KeywordRecaller(storage)
    vector_recaller = VectorRecaller(storage)
    discloser = TruncatingDiscloser()
    unit_reader = UnitReader(kv)
    reranker = OverlapReranker(tokenizer) if rerank else None
    retriever = PipelineRetriever(
        parser,
        [keyword, vector_recaller],
        RRFFuser(),
        discloser,
        unit_reader,
        reranker,
        storage=storage,
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
    supersedes: str = "",
    tags: Optional[list[str]] = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(t_event=t_event, t_valid=t_valid, t_invalid=t_invalid),
        supersedes=supersedes,
        tags=list(tags or []),
        lifecycle=lifecycle,
    )


def index_unit(world: RetrievalWorld, unit: MemoryUnit) -> None:
    """Mirror the minimal write-side indexing needed by retrieval tests."""
    world.kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    world.vector.insert(
        unit.scope,
        [VectorRecord(id=unit.id, vector=world.embedder.embed_query(unit.content))],
    )
    world.fulltext.insert(unit.scope, [Document(id=unit.id, text=unit.content)])


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
