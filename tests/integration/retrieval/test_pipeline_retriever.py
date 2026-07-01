"""PipelineRetriever integration tests."""

from __future__ import annotations

from time import perf_counter, sleep
from typing import List

import pytest

from common.errors import BackendError, ValidationError
from common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
    KeywordFeatureExtractor,
)
from common.reranker.reranker_impl.overlap_reranker import OverlapReranker
from common.type_def import FilterClause, FilterOp
from common.type_def.memory import LifecycleState
from common.type_def.scope import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.discloser_impl.structured_discloser import StructuredDiscloser
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.fuser_impl.weighted_rrf_fuser import WeightedRRFFuser
from retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from retrieval.recaller import Recaller
from retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from retrieval.types import (
    DisclosureLevel,
    ParsedQuery,
    RecallChannel,
    RetrievalQuery,
    ScoredUnit,
)
from tests.conftest import DEFAULT_SCOPE, index_unit, make_unit, make_world

pytestmark = pytest.mark.integration


class FailingRecaller(Recaller):
    """Fault-injection recaller used to verify per-channel degradation."""

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.VECTOR

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        raise BackendError("simulated backend outage")


class StaticRecaller(Recaller):
    """Fixed candidate recaller used to verify post-rerank filtering."""

    def __init__(
        self,
        candidates: List[ScoredUnit],
        channel: RecallChannel = RecallChannel.KEYWORD,
    ) -> None:
        self._candidates = candidates
        self._channel = channel

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return self._channel

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        return self._candidates[:top_k]


class SlowRecaller(Recaller):
    """Delayed recaller used to verify channel-level parallelism."""

    def __init__(self, channel: RecallChannel, unit_id: str, delay_seconds: float) -> None:
        self._channel = channel
        self._unit_id = unit_id
        self._delay_seconds = delay_seconds

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return self._channel

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        sleep(self._delay_seconds)
        return [ScoredUnit(self._unit_id, 1.0, self._channel)]


@pytest.fixture
def indexed_world(world, unit_factory, index_unit_fn):
    index_unit_fn(world, unit_factory("u1", "alice likes coffee"))
    index_unit_fn(world, unit_factory("u2", "bob likes tea"))
    return world


def test_end_to_end_recall(indexed_world, scope) -> None:
    result = indexed_world.retriever.retrieve(
        scope,
        RetrievalQuery(text="coffee", top_k=5, with_trajectory=True),
    )

    assert "u1" in [item.unit_id for item in result.items]
    assert result.trajectory


def test_empty_query_returns_empty(indexed_world, scope) -> None:
    result = indexed_world.retriever.retrieve(scope, RetrievalQuery(text="   ", top_k=5))

    assert result.items == []


def test_noise_only_query_short_circuits_after_parse(indexed_world, scope) -> None:
    parser = SimpleQueryParser(
        indexed_world.tokenizer,
        indexed_world.embedder,
        feature_extractor=KeywordFeatureExtractor(indexed_world.tokenizer),
        sanitize=True,
    )
    retriever = PipelineRetriever(
        parser,
        [indexed_world.keyword, indexed_world.vector_recaller],
        RRFFuser(),
        indexed_world.discloser,
        indexed_world.unit_reader,
    )

    result = retriever.retrieve(
        scope,
        RetrievalQuery(text="[Fri 2026-03-27 06:16 UTC]", top_k=5, with_trajectory=True),
    )

    stages = [step.stage for step in result.trajectory]
    parse_steps = [step for step in result.trajectory if step.stage == "parse"]
    assert result.items == [], "噪声 query 清洗为空后应返回空结果"
    assert "recall" not in stages, "清洗为空后不应触发任何召回通道"
    assert parse_steps[0].detail["skipped"] == "empty_after_parse", "轨迹应记录解析后空短路"


def test_invalid_top_k_raises(indexed_world, scope) -> None:
    with pytest.raises(ValidationError):
        indexed_world.retriever.retrieve(scope, RetrievalQuery(text="coffee", top_k=0))


def test_invalid_max_tokens_raises(indexed_world, scope) -> None:
    with pytest.raises(ValidationError):
        indexed_world.retriever.retrieve(
            scope,
            RetrievalQuery(
                text="coffee",
                disclosure=DisclosureLevel.ADAPTIVE,
                max_tokens=0,
            ),
        )


def test_excludes_superseded_end_to_end(indexed_world, scope, unit_factory, index_unit_fn) -> None:
    index_unit_fn(
        indexed_world, unit_factory("old", "coffee old", lifecycle=LifecycleState.SUPERSEDED)
    )

    result = indexed_world.retriever.retrieve(scope, RetrievalQuery(text="coffee", top_k=10))

    ids = {item.unit_id for item in result.items}
    assert "u1" in ids
    assert "old" not in ids


def test_include_archived_option(indexed_world, scope, unit_factory, index_unit_fn) -> None:
    index_unit_fn(
        indexed_world, unit_factory("arc", "coffee archived", lifecycle=LifecycleState.ARCHIVED)
    )

    default = indexed_world.retriever.retrieve(scope, RetrievalQuery(text="coffee", top_k=10))
    opened = indexed_world.retriever.retrieve(
        scope, RetrievalQuery(text="coffee", top_k=10, include_archived=True)
    )

    assert "arc" not in {item.unit_id for item in default.items}
    assert "arc" in {item.unit_id for item in opened.items}


def test_channels_override_skips_other_channels(indexed_world, scope) -> None:
    result = indexed_world.retriever.retrieve(
        scope,
        RetrievalQuery(
            text="coffee", top_k=5, channels=[RecallChannel.KEYWORD], with_trajectory=True
        ),
    )

    recalled = {step.channel for step in result.trajectory if step.stage == "recall"}
    assert recalled == {RecallChannel.KEYWORD}


def test_trajectory_records_stages_and_cost(indexed_world, scope) -> None:
    result = indexed_world.retriever.retrieve(
        scope, RetrievalQuery(text="coffee", top_k=5, with_trajectory=True)
    )

    stages = {step.stage for step in result.trajectory}
    assert "recheck" in stages
    fuse = [step for step in result.trajectory if step.stage == "fuse"][0]
    assert fuse.detail["strategy"] == "rrf"
    assert all(step.cost_ms >= 0.0 for step in result.trajectory)


def test_channel_failure_isolated(indexed_world, scope) -> None:
    retriever = PipelineRetriever(
        indexed_world.parser,
        [FailingRecaller(), indexed_world.keyword],
        RRFFuser(),
        indexed_world.discloser,
        indexed_world.unit_reader,
    )

    result = retriever.retrieve(scope, RetrievalQuery(text="coffee", top_k=5, with_trajectory=True))

    assert "u1" in [item.unit_id for item in result.items]
    degraded = [step for step in result.trajectory if step.detail.get("degraded")]
    assert degraded
    assert degraded[0].detail["degraded"] == "BackendError"


def test_recall_channels_run_in_parallel(scope) -> None:
    world = make_world()
    index_unit(world, make_unit("u1", "alice likes coffee"))
    index_unit(world, make_unit("u2", "bob likes tea"))
    retriever = PipelineRetriever(
        world.parser,
        [
            SlowRecaller(RecallChannel.KEYWORD, "u1", 0.10),
            SlowRecaller(RecallChannel.VECTOR, "u2", 0.10),
        ],
        RRFFuser(),
        world.discloser,
        world.unit_reader,
    )

    t0 = perf_counter()
    result = retriever.retrieve(
        scope, RetrievalQuery(text="coffee tea", top_k=5, with_trajectory=True)
    )
    elapsed = perf_counter() - t0

    assert elapsed < 0.17
    assert {item.unit_id for item in result.items} == {"u1", "u2"}
    recall_steps = [step for step in result.trajectory if step.stage == "recall"]
    assert [step.channel for step in recall_steps] == [
        RecallChannel.KEYWORD,
        RecallChannel.VECTOR,
    ]


def test_rerank_toggle_per_call() -> None:
    world = make_world(rerank=True)
    index_unit(world, make_unit("u1", "alice likes coffee"))

    on = world.retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="coffee", top_k=5, with_trajectory=True)
    )
    off = world.retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="coffee", top_k=5, rerank=False, with_trajectory=True)
    )

    assert "rerank" in {step.stage for step in on.trajectory}
    assert "rerank" not in {step.stage for step in off.trajectory}


def test_rerank_filters_zero_score_candidates() -> None:
    world = make_world()
    index_unit(world, make_unit("hit", "coffee"))
    index_unit(world, make_unit("noise", "tea"))
    retriever = PipelineRetriever(
        world.parser,
        [
            StaticRecaller(
                [
                    ScoredUnit("hit", 1.0, RecallChannel.KEYWORD),
                    ScoredUnit("noise", 0.9, RecallChannel.KEYWORD),
                ]
            )
        ],
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        OverlapReranker(world.tokenizer),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="coffee", top_k=5, with_trajectory=True)
    )

    assert [item.unit_id for item in result.items] == ["hit"]
    score_filter = [step for step in result.trajectory if step.stage == "score_filter"][0]
    assert score_filter.detail["dropped"] == "1"


def test_adaptive_disclosure_records_actual_levels_in_trajectory() -> None:
    world = make_world()
    index_unit(
        world,
        make_unit("u1", "packages/foo package manager is pnpm. dependency installs use pnpm."),
    )
    index_unit(
        world,
        make_unit("u2", "root workspace package manager is npm. dependency installs use npm."),
    )
    retriever = PipelineRetriever(
        world.parser,
        [
            StaticRecaller(
                [
                    ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                    ScoredUnit("u2", 1.0, RecallChannel.KEYWORD),
                ]
            )
        ],
        RRFFuser(),
        StructuredDiscloser(),
        world.unit_reader,
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE,
        RetrievalQuery(
            text="package manager",
            top_k=2,
            disclosure=DisclosureLevel.ADAPTIVE,
            max_tokens=1000,
            with_trajectory=True,
        ),
    )

    assert [item.level for item in result.items] == [DisclosureLevel.L1, DisclosureLevel.L1]
    disclose = [step for step in result.trajectory if step.stage == "disclose"][0]
    assert disclose.detail["mode"] == "adaptive"
    assert disclose.detail["max_tokens"] == "1000"
    assert disclose.detail["levels"] == "l1,l1"
    assert int(disclose.detail["estimated_tokens"]) > 0


def test_weighted_rrf_and_structured_discloser_run_in_pipeline() -> None:
    world = make_world()
    index_unit(
        world,
        make_unit(
            "keyword_hit",
            "packages/foo package manager is pnpm. dependency installs must run there.",
        ),
    )
    index_unit(
        world,
        make_unit(
            "vector_hit",
            "root workspace package manager is npm. dependency installs run from root.",
        ),
    )
    retriever = PipelineRetriever(
        world.parser,
        [
            StaticRecaller(
                [ScoredUnit("keyword_hit", 0.3, RecallChannel.KEYWORD)],
                RecallChannel.KEYWORD,
            ),
            StaticRecaller(
                [ScoredUnit("vector_hit", 0.99, RecallChannel.VECTOR)],
                RecallChannel.VECTOR,
            ),
        ],
        WeightedRRFFuser(
            k=0,
            channel_weights={RecallChannel.KEYWORD: 2.0, RecallChannel.VECTOR: 1.0},
        ),
        StructuredDiscloser(),
        world.unit_reader,
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE,
        RetrievalQuery(
            text="package manager pnpm",
            top_k=2,
            disclosure=DisclosureLevel.L1,
            with_trajectory=True,
        ),
    )

    assert [item.unit_id for item in result.items] == ["keyword_hit", "vector_hit"]
    fuse = [step for step in result.trajectory if step.stage == "fuse"][0]
    assert fuse.detail == {
        "strategy": "weighted_rrf",
        "rrf_k": "0",
        "channel_weights": "keyword=2,vector=1",
    }
    top_content = result.items[0].content
    assert "[summary] packages/foo package manager is pnpm." in top_content
    assert "[evidence] packages/foo package manager is pnpm." in top_content
    assert "[why] keyword(rank=1,score=0.3,weight=2,contribution=2)" in top_content


def test_user_tag_filter_applied_end_to_end(
    indexed_world, scope, unit_factory, index_unit_fn
) -> None:
    """调用方 filters 后置生效：Store 忽略下推时，检索层读真源兜底过滤（G2）。"""
    index_unit_fn(indexed_world, unit_factory("w", "coffee work note", tags=["work"]))

    result = indexed_world.retriever.retrieve(
        scope,
        RetrievalQuery(
            text="coffee",
            top_k=10,
            filters=[FilterClause("tags", FilterOp.CONTAINS, "work")],
        ),
    )

    ids = {item.unit_id for item in result.items}
    assert "w" in ids  # 命中且带 work 标签
    assert "u1" not in ids  # 命中 coffee 但无 work 标签 → 被后置过滤剔除
