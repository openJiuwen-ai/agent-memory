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
from common.security.request_context import new_request_context
from common.security.types import AuthContext, RequestSecurityContext, Role, Surface
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
from storage.types import Document, VectorRecord
from storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

DEFAULT_SCOPE = Scope(org="acme", user="u1", agent="a1", session="s1")

#: 具名 ROOT 主体。管理面测试用它，**不用**空 ``Scope()``——空 actor 现在是
#: 「上下文不完整」的信号，PDP 对它直接拒（S08 不变量 21）。
ROOT_ACTOR = Scope(org="system", user="root")


def sec(
    actor: Scope,
    *,
    role: Role = Role.USER,
    surface: Surface = Surface.SDK,
) -> RequestSecurityContext:
    """把一个 actor Scope 包成测试用的 ``RequestSecurityContext``。

    测试要表达的几乎总是「谁在调用」，而 ``RequestSecurityContext`` 还带着
    request_id、started_at、surface 这些由服务端产生的字段。走
    ``new_request_context`` 而不是直接构造，是为了让测试和生产路径用同一个
    受控入口——那三个字段的产生规则只有一处实现（S08 不变量 32）。

    ``role`` 默认 ``USER``：管理面权限必须由用例显式声明
    ``role=Role.ROOT`` 才拿得到，默认给 ROOT 会让「谁能碰管理面」这条断言
    在所有用例里静默失效。
    """
    return new_request_context(AuthContext(actor=actor, role=role), surface=surface)


def root_sec(actor: Scope = ROOT_ACTOR) -> RequestSecurityContext:
    """管理面测试用的 ROOT 安全上下文。ROOT 由 ``role`` 表达，不由 actor 形状表达。"""
    return sec(actor, role=Role.ROOT)


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
    parser = SimpleQueryParser(tokenizer, embedder, feature_extractor=features)
    keyword = KeywordRecaller(fulltext)
    vector_recaller = VectorRecaller(vector)
    discloser = TruncatingDiscloser()
    unit_reader = UnitReader(kv)
    reranker = OverlapReranker(tokenizer) if rerank else None
    retriever = PipelineRetriever(
        parser, [keyword, vector_recaller], RRFFuser(), discloser, unit_reader, reranker
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
