"""层级查询测试：真实索引/存储接线与故意不识别新字段的 parser。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    RankedStorageResult,
    RecallChannel,
    RetrievalPipeline,
    Scope,
    ScoredMemoryUnit,
    Segment,
)
from jiuwen_memory.construction.index_builder_impl.fulltext_index_builder import (
    FulltextIndexBuilder,
)
from jiuwen_memory.construction.index_builder_impl.vector_index_builder import VectorIndexBuilder
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.discloser_impl.truncating_discloser import TruncatingDiscloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser import QueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RetrievalQuery
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.domain_store_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.storage.domain_store_impl.vector_recaller import VectorRecaller
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore

QUERY_SCOPE = Scope(org="org", space="space", user="alice", agent="assistant")
SPAN_START = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
SPAN_END = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)


class ConstantEmbedder(Embedder):
    """等分向量保证 top-k 前的结构过滤是唯一能找回尾部节点的条件。"""

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.EMBEDDER

    @staticmethod
    def health() -> None:
        return None

    @staticmethod
    def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    @staticmethod
    def dimension() -> int:
        return 2


class LegacyParser(QueryParser):
    """不复制 hierarchy 新字段，测试编排层的显式透传责任。"""

    def __init__(self) -> None:
        self.event_window: tuple[datetime | None, datetime | None] = (None, None)

    @staticmethod
    def operator_type() -> RetrievalOperatorType:
        return RetrievalOperatorType.QUERY_PARSER

    @staticmethod
    def health() -> None:
        return None

    def parse(self, query: RetrievalQuery) -> ParsedQuery:
        return ParsedQuery(
            raw=query.text,
            rewritten=query.text,
            keywords=query.text.split(),
            vector=[1.0, 0.0],
            scalar_filters=query.filters,
            as_of=query.as_of,
            time_from=self.event_window[0],
            time_to=self.event_window[1],
        )


class UncheckedRankedDomain(CompositeDomainStore):
    """模拟第三方数据面返回未复核本体的融合结果。"""

    def retrieve(self, scope, query, fuser, **kwargs) -> RankedStorageResult:
        """刻意漏掉真源约束，验证 Retriever 的最后边界。"""
        units = self.list(scope).items
        candidates = [ScoredMemoryUnit(unit, 1.0, RecallChannel.KEYWORD) for unit in units]
        return RankedStorageResult(candidates=candidates[:kwargs["rank_limit"]])


@dataclass
class HierarchyHarness:
    retriever: PipelineRetriever
    domain: CompositeDomainStore
    parser: LegacyParser
    manager: CompositeStoreManager
    keyword_builder: FulltextIndexBuilder
    vector_builder: VectorIndexBuilder

    def add(self, units: list[MemoryUnit]) -> None:
        self.domain.add(QUERY_SCOPE, units)
        self.keyword_builder.build(units)
        self.vector_builder.build(units)


def make_harness(
    pipeline: RetrievalPipeline,
    *,
    recall_limit: int = 50,
    unchecked_ranked: bool = False,
) -> HierarchyHarness:
    """使用真实关键词/向量索引与三种数据面执行路径。"""
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(),
        fulltext=InMemoryFulltextStore(WhitespaceTokenizer()),
        vector=InMemoryVectorStore(),
    )
    domain_type = UncheckedRankedDomain if unchecked_ranked else CompositeDomainStore
    domain = domain_type(manager=manager, preferred_pipeline=pipeline)
    domain.bind_recallers([KeywordRecaller(manager), VectorRecaller(manager)])
    manager.bind_domain_store(domain)
    parser = LegacyParser()
    retriever = PipelineRetriever(
        parser,
        RRFFuser(),
        TruncatingDiscloser(),
        None,
        over_fetch_factor=1,
        over_fetch_floor=1,
        recall_max=recall_limit,
        domain_store=domain,
    )
    return HierarchyHarness(
        retriever,
        domain,
        parser,
        manager,
        FulltextIndexBuilder(manager),
        VectorIndexBuilder(manager, FixedWindowChunker(), ConstantEmbedder()),
    )


def tree_unit(unit_id: str, role: HierarchyRole = HierarchyRole.TIME_SPAN) -> MemoryUnit:
    """创建有合法结构时间、可被两个真实索引召回的节点。"""
    return MemoryUnit(
        id=unit_id,
        scope=QUERY_SCOPE,
        segments=[Segment(content="recall evidence")],
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME,
            role=role,
            span_start=SPAN_START,
            span_end=SPAN_END,
        ),
    )


def hierarchy_query(**kwargs) -> RetrievalQuery:
    """默认只查询 TIME/time_span；单项用例显式覆盖条件。"""
    values = {
        "text": "recall evidence",
        "hierarchy_kind": HierarchyKind.TIME,
        "hierarchy_role": HierarchyRole.TIME_SPAN,
        "span_start": SPAN_START,
        "span_end": SPAN_END,
        "top_k": 20,
    }
    values.update(kwargs)
    return RetrievalQuery(**values)
