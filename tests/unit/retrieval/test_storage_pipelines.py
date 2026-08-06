"""Retriever 对统一 Storage 三条 pipeline 的选择与等价行为。"""

from __future__ import annotations

import pytest

from common.errors import StorageRetrievalError, ValidationError
from common.type_def import (
    MemoryUnit,
    ParsedQuery,
    RetrievalPipeline,
    Scope,
    ScoredCandidate,
    ScoredMemoryUnit,
    Segment,
)
from retrieval.base import RetrievalOperatorType
from retrieval.discloser import Discloser
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.query_parser import QueryParser
from retrieval.recaller import Recaller
from retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from retrieval.retriever_impl.unit_reader import UnitReader
from retrieval.types import (
    DisclosureLevel,
    RecallChannel,
    RetrievalQuery,
    RetrievedItem,
    ScoredUnit,
)
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from storage.storage_impl import CompositeStorage

pytestmark = pytest.mark.unit


class StaticParser(QueryParser):
    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.QUERY_PARSER

    def health(self) -> None:
        return None

    def parse(self, query: RetrievalQuery) -> ParsedQuery:
        return ParsedQuery(raw=query.text, rewritten=query.text)


class IdRecaller(Recaller):
    def __init__(self, channel: RecallChannel, unit_id: str, *, fail: bool = False) -> None:
        self._channel = channel
        self._unit_id = unit_id
        self._fail = fail

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return self._channel

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        if self._fail:
            raise RuntimeError("backend unavailable")
        return [ScoredUnit(self._unit_id, 1.0, self._channel)]


class SimpleDiscloser(Discloser):
    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.DISCLOSER

    def health(self) -> None:
        return None

    def disclose(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        units: dict[str, MemoryUnit],
        level: DisclosureLevel,
        max_tokens: int | None = None,
    ) -> list[RetrievedItem]:
        return [
            RetrievedItem(
                unit_id=candidate.unit_id,
                score=candidate.score,
                content=units[candidate.unit_id].content,
                level=level,
            )
            for candidate in candidates
        ]


class CountingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get(self, scope: Scope, key: str) -> bytes:
        self.get_calls += 1
        return super().get(scope, key)


def _build_retriever(
    pipeline: RetrievalPipeline,
    recallers: list[Recaller],
) -> tuple[PipelineRetriever, CompositeStorage, CountingKVStore, Scope]:
    scope = Scope(org="org", space="space", user="user")
    kv = CountingKVStore()
    storage = CompositeStorage(
        kv=kv,
        recallers=recallers,
        preferred_pipeline=pipeline,
    )
    storage.add(scope, [MemoryUnit(id="u1", scope=scope, segments=[Segment(content="one")])])
    kv.get_calls = 0
    retriever = PipelineRetriever(
        StaticParser(),
        recallers,
        RRFFuser(),
        SimpleDiscloser(),
        UnitReader(kv),
        rerank_max=10,
        storage=storage,
    )
    return retriever, storage, kv, scope


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
def test_all_storage_pipelines_return_equivalent_materialized_results(
    pipeline: RetrievalPipeline,
) -> None:
    recallers = [
        IdRecaller(RecallChannel.KEYWORD, "u1"),
        IdRecaller(RecallChannel.VECTOR, "u1"),
    ]
    retriever, _, kv, scope = _build_retriever(pipeline, recallers)

    result = retriever.retrieve(scope, RetrievalQuery(text="one", top_k=1))

    assert [item.unit_id for item in result.items] == ["u1"]
    assert len(result.errors) == 0
    assert kv.get_calls == 1, "跨通道重复 id 应只读取一次真源"


def test_partial_channel_failure_returns_items_and_structured_error() -> None:
    recallers = [
        IdRecaller(RecallChannel.KEYWORD, "u1"),
        IdRecaller(RecallChannel.VECTOR, "u1", fail=True),
    ]
    retriever, _, _, scope = _build_retriever(
        RetrievalPipeline.RECALL_GET_RANK, recallers
    )

    result = retriever.retrieve(scope, RetrievalQuery(text="one", top_k=1))

    assert [item.unit_id for item in result.items] == ["u1"]
    assert len(result.errors) == 1
    assert result.errors[0].channel == RecallChannel.VECTOR
    assert result.errors[0].error_type == "RuntimeError"


def test_fuser_preserves_materialized_unit_and_cross_channel_evidence() -> None:
    scope = Scope(org="org")
    unit = MemoryUnit(id="u1", scope=scope)
    candidates = [
        [ScoredMemoryUnit(unit, 0.8, RecallChannel.KEYWORD)],
        [ScoredMemoryUnit(unit, 0.9, RecallChannel.VECTOR)],
    ]

    fused = RRFFuser().fuse(ParsedQuery(raw="one"), candidates)

    assert isinstance(fused[0], ScoredMemoryUnit)
    assert fused[0].unit is unit
    assert {evidence.channel for evidence in fused[0].evidence} == {
        RecallChannel.KEYWORD,
        RecallChannel.VECTOR,
    }


def test_all_selected_channels_failing_raises_storage_retrieval_error() -> None:
    recallers = [IdRecaller(RecallChannel.KEYWORD, "u1", fail=True)]
    retriever, _, _, scope = _build_retriever(
        RetrievalPipeline.RECALL_GET_RANK, recallers
    )

    with pytest.raises(StorageRetrievalError):
        retriever.retrieve(scope, RetrievalQuery(text="one", top_k=1))


def test_explicit_empty_channels_are_invalid() -> None:
    retriever, _, _, scope = _build_retriever(
        RetrievalPipeline.RECALL_GET_RANK,
        [IdRecaller(RecallChannel.KEYWORD, "u1")],
    )

    with pytest.raises(ValidationError):
        retriever.retrieve(scope, RetrievalQuery(text="one", channels=[]))
