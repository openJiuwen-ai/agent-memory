"""PipelineRetriever integration tests."""

from __future__ import annotations

from time import perf_counter, sleep
from types import SimpleNamespace
from typing import List

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.embedder.base import EmbedderProducer
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.feature_extractor.feature_extractor_impl.keyword_feature_extractor import (
    KeywordFeatureExtractor,
)
from jiuwen_memory.common.reranker.base import Reranker
from jiuwen_memory.common.reranker.reranker_impl.overlap_reranker import OverlapReranker
from jiuwen_memory.common.type_def import FilterClause, FilterOp, RetrievalPipeline, memory_key
from jiuwen_memory.common.type_def.memory import LifecycleState
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.common.type_def.scope import Scope
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.discloser_impl.structured_discloser import StructuredDiscloser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.fuser_impl.weighted_rrf_fuser import WeightedRRFFuser
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.recaller import Recaller, RecallerProducer
from jiuwen_memory.retrieval.recaller_impl.keyword_recaller import KeywordRecaller
from jiuwen_memory.retrieval.recaller_impl.vector_recaller import VectorRecaller
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import (
    DisclosureLevel,
    ParsedQuery,
    RecallChannel,
    RetrievalQuery,
    ScoredUnit,
)
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.fulltext import FulltextProducer
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.domain_store_impl import CompositeDomainStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.vector import VectorProducer
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
        self.calls: List[int] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return self._channel

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        self.calls.append(top_k)
        return self._candidates[:top_k]


class StaticReranker(Reranker):
    """Deterministic reranker returning predefined scores in input order."""

    def __init__(self, scores: List[float]) -> None:
        self._scores = scores

    def plugin_type(self) -> PluginType:
        return PluginType.RERANKER

    def health(self) -> None:
        return None

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        return self._scores[: len(texts)]


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


def _storage_for(world, recallers: list) -> CompositeDomainStore:
    """复用 world 的各 Store 建双面栈（manager + 数据面），绑定测试 recaller。"""
    manager = CompositeStoreManager(
        kv=world.kv, vector=world.vector, fulltext=world.fulltext
    )
    domain_store = CompositeDomainStore(
        manager=manager, preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK
    )
    domain_store.bind_recallers(recallers)
    manager.bind_domain_store(domain_store)
    return domain_store


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
        RRFFuser(),
        indexed_world.discloser,
        indexed_world.unit_reader,
        domain_store=_storage_for(indexed_world, []),
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
        RRFFuser(),
        indexed_world.discloser,
        indexed_world.unit_reader,
        domain_store=_storage_for(indexed_world, [FailingRecaller(), indexed_world.keyword]),
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
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        domain_store=_storage_for(
            world,
            [
                SlowRecaller(RecallChannel.KEYWORD, "u1", 0.10),
                SlowRecaller(RecallChannel.VECTOR, "u2", 0.10),
            ],
        ),
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
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        OverlapReranker(world.tokenizer),
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("hit", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("noise", 0.9, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="coffee", top_k=5, with_trajectory=True)
    )

    assert [item.unit_id for item in result.items] == ["hit"]
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["dropped"] == "1"


def test_min_score_applies_when_reranked() -> None:
    world = make_world()
    for uid in ["u1", "u2", "u3"]:
        index_unit(world, make_unit(uid, f"{uid} candidate"))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        StaticReranker([0.9, 0.45, 0.1]),
        min_score=0.5,
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("u2", 0.9, RecallChannel.KEYWORD),
                        ScoredUnit("u3", 0.8, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=3, with_trajectory=True)
    )

    assert [item.unit_id for item in result.items] == ["u1"], "精排后应按绝对阈值欠填"
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "True"
    assert threshold.detail["passed"] == "1"


def test_min_score_skipped_when_rerank_disabled() -> None:
    world = make_world()
    for uid in ["u1", "u2"]:
        index_unit(world, make_unit(uid, f"{uid} candidate"))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        StaticReranker([0.01, 0.01]),
        min_score=0.4,
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("u2", 0.9, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE,
        RetrievalQuery(text="candidate", top_k=2, rerank=False, with_trajectory=True),
    )

    assert [item.unit_id for item in result.items] == ["u1", "u2"]
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "False"


def test_threshold_under_fills_below_top_k() -> None:
    world = make_world()
    for uid in ["u1", "u2", "u3"]:
        index_unit(world, make_unit(uid, f"{uid} candidate"))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        StaticReranker([0.95, 0.7, 0.2]),
        min_score_ratio=0.8,
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("u2", 0.9, RecallChannel.KEYWORD),
                        ScoredUnit("u3", 0.8, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=3, with_trajectory=True)
    )

    assert [item.unit_id for item in result.items] == ["u1"]
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["dropped"] == "2"


def test_budget_expands_to_top_k() -> None:
    # rerank_max 小于 top_k 时，精排预算自动扩展到 top_k，避免静默欠召。
    world = make_world()
    candidates = []
    for index in range(1, 5):
        uid = f"u{index}"
        index_unit(world, make_unit(uid, f"{uid} candidate"))
        candidates.append(ScoredUnit(uid, 1.0, RecallChannel.KEYWORD))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        over_fetch_factor=1,
        over_fetch_floor=1,
        rerank_max=2,
        domain_store=_storage_for(world, [StaticRecaller(candidates)]),
    )

    result = retriever.retrieve(DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=4))

    assert [item.unit_id for item in result.items] == ["u1", "u2", "u3", "u4"]


def test_over_fetch_recall_width() -> None:
    # 每路召回量 = max(top_k*factor, floor)，撒宽网喂融合。
    world = make_world()
    candidates = []
    for index in range(1, 7):
        uid = f"u{index}"
        index_unit(world, make_unit(uid, f"{uid} candidate"))
        candidates.append(ScoredUnit(uid, 1.0, RecallChannel.KEYWORD))

    factor_driven = StaticRecaller(candidates)
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        over_fetch_factor=3,
        over_fetch_floor=1,
        domain_store=_storage_for(world, [factor_driven]),
    )
    result = retriever.retrieve(DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=2))
    assert factor_driven.calls == [6], "factor 主导：max(2*3, 1) = 6"
    assert [item.unit_id for item in result.items] == ["u1", "u2"]

    floor_driven = StaticRecaller(candidates)
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        over_fetch_factor=1,
        over_fetch_floor=5,
        domain_store=_storage_for(world, [floor_driven]),
    )
    retriever.retrieve(DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=2))
    assert floor_driven.calls == [5], "floor 主导：max(2*1, 5) = 5"


def test_recall_max_caps_recall_k() -> None:
    # 超大 top_k 经 factor 放大后被 recall_max 硬上限封顶，保护后端。
    world = make_world()
    recaller = StaticRecaller([])
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        over_fetch_factor=4,
        over_fetch_floor=60,
        recall_max=100,
        domain_store=_storage_for(world, [recaller]),
    )

    retriever.retrieve(DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=50))

    # max(50*4, 60) = 200，被 recall_max=100 封顶。
    assert recaller.calls == [100], "recall_k 应被 recall_max 硬上限封顶"


def test_direct_constructor_default_recall_max_caps_recall_k() -> None:
    # 直接构造也应使用出厂默认 recall_max=100，避免绕过工厂后后端召回压力失控。
    world = make_world()
    recaller = StaticRecaller([])
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        domain_store=_storage_for(world, [recaller]),
    )

    retriever.retrieve(DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=50))

    assert recaller.calls == [100], "直接构造默认值应与工厂出厂默认 recall_max=100 一致"


def test_retrieval_over_fetch_read_from_config() -> None:
    candidates = [
        ScoredUnit(f"u{index}", 1.0, RecallChannel.KEYWORD)
        for index in range(1, 12)
    ]
    recaller = StaticRecaller(candidates)

    def build_recording_recaller(_config):
        return recaller

    RecallerProducer.register("recording_config_test")(build_recording_recaller)

    raw = default_config_dict()
    raw["globals"]["graph_enabled"] = False
    raw["globals"]["vector_enabled"] = False
    # recaller 选择键归 store_manager 命名空间：覆盖 default 实例的 keyword_recaller 装配。
    raw["store_manager"]["default"]["params"]["keyword_recaller"] = {
        "target": "recording_config_test"
    }
    params = raw["retriever"]["default"]["params"]
    params["over_fetch_factor"] = 3
    params["over_fetch_floor"] = 7
    params["recall_max"] = 11
    params["rerank_max"] = 9
    ctx = AssemblyContext.from_dict(raw)

    register_plugins()
    register_backends()
    register_operators()
    Factory.reset_all()
    try:
        retriever = RetrieverProducer.build_named("default", ctx)
        kv = KvProducer.build_named("default", ctx)
        for candidate in candidates:
            kv.insert(
                DEFAULT_SCOPE,
                memory_key(candidate.unit_id),
                dumps(make_unit(candidate.unit_id, f"{candidate.unit_id} candidate")),
            )
        result = retriever.retrieve(
            DEFAULT_SCOPE,
            RetrievalQuery(text="candidate", top_k=4, with_trajectory=True),
        )
    finally:
        Factory.reset_all()

    assert isinstance(retriever, PipelineRetriever)
    assert recaller.calls == [11], "max(4*3, 7)=12，应被 recall_max=11 封顶"
    recheck = [step for step in result.trajectory if step.stage == "recheck"][0]
    assert recheck.candidate_count == 9, "rerank_max=9 应控制进入 recheck 的预算"


def test_configured_calibrated_threshold_active() -> None:
    # 校准路径（走了 rerank）配置相对阈值 0.6 时按比例裁剪，而不是只保留正分门。
    # 出厂默认已改为 0.0（见 test_direct_constructor_threshold_off_by_default），
    # 故此处显式传参——本用例锁的是阈值机制，不是某个默认值。
    world = make_world()
    for uid in ["u1", "u2"]:
        index_unit(world, make_unit(uid, f"{uid} candidate"))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        StaticReranker([0.95, 0.2]),
        min_score_ratio=0.6,
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("u2", 0.9, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="candidate", top_k=2, with_trajectory=True)
    )

    assert [item.unit_id for item in result.items] == ["u1"], "低于 0.6×最高分的候选应被裁剪"
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "True"
    assert threshold.detail["min_score_ratio"] == "0.6"
    assert threshold.detail["dropped"] == "1"


def test_configured_uncalibrated_threshold_active() -> None:
    # 未精排路径按 min_score_ratio_uncalibrated 走较松的相对阈值（此处配 0.3）。
    world = make_world()
    candidates = []
    for index in range(1, 5):
        uid = f"u{index}"
        index_unit(world, make_unit(uid, f"{uid} candidate"))
        candidates.append(ScoredUnit(uid, 1.0, RecallChannel.KEYWORD))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(k=0),
        world.discloser,
        world.unit_reader,
        min_score_ratio_uncalibrated=0.3,
        domain_store=_storage_for(world, [StaticRecaller(candidates)]),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE,
        RetrievalQuery(text="candidate", top_k=4, rerank=False, with_trajectory=True),
    )

    assert [item.unit_id for item in result.items] == ["u1", "u2", "u3"]
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "False"
    assert threshold.detail["min_score_ratio"] == "0.3"
    assert threshold.detail["dropped"] == "1"


def test_direct_constructor_threshold_off_by_default() -> None:
    # 直接构造的出厂默认：校准/未校准两路相对阈值均为 0，不按比例裁剪。
    # 锁定默认值本身——与上面两个显式配置用例互补。
    world = make_world()
    candidates = []
    for index in range(1, 5):
        uid = f"u{index}"
        index_unit(world, make_unit(uid, f"{uid} candidate"))
        candidates.append(ScoredUnit(uid, 1.0, RecallChannel.KEYWORD))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(k=0),
        world.discloser,
        world.unit_reader,
        domain_store=_storage_for(world, [StaticRecaller(candidates)]),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE,
        RetrievalQuery(text="candidate", top_k=4, rerank=False, with_trajectory=True),
    )

    assert [item.unit_id for item in result.items] == ["u1", "u2", "u3", "u4"]
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["min_score_ratio"] == "0", "出厂默认相对阈值应为 0"
    assert threshold.detail["dropped"] == "0"


def test_configured_threshold_active_end_to_end() -> None:
    # 生产装配 + 显式开启相对阈值（ratio 0.6）的端到端行为：低于最高分 60% 的候选
    # 被裁剪，相关结果不被清空。补齐「测试构造走阈值全关、生产装配走阈值开」的
    # 覆盖缺口。出厂默认已改为 0.0（见 test_default_config_threshold_off_end_to_end），
    # 故此处显式配置——本用例锁的是阈值机制本身，不是某个默认值。
    raw = default_config_dict()
    raw["globals"]["graph_enabled"] = False
    raw["retriever"]["default"]["params"]["min_score_ratio"] = 0.6
    ctx = AssemblyContext.from_dict(raw)

    register_plugins()
    register_backends()
    register_operators()
    Factory.reset_all()
    try:
        retriever = RetrieverProducer.build_named("default", ctx)
        # 经各 Producer 取与 retriever 共享的同一批 store/embedder 实例做写侧索引。
        stack = SimpleNamespace(
            kv=KvProducer.build_named("default", ctx),
            vector=VectorProducer.build_named("default", ctx),
            fulltext=FulltextProducer.build_named("default", ctx),
            embedder=EmbedderProducer.build_named("default", ctx),
        )
        index_unit(stack, make_unit("u1", "alice likes coffee"))
        index_unit(stack, make_unit("u2", "bob drinks coffee"))

        result = retriever.retrieve(
            DEFAULT_SCOPE,
            RetrievalQuery(text="alice coffee", top_k=10, with_trajectory=True),
        )
    finally:
        Factory.reset_all()

    # overlap 精排分：u1 命中 2/3 查询词、u2 命中 1/3 → 0.6 ratio 裁掉 u2、保留 u1。
    assert [item.unit_id for item in result.items] == ["u1"], "阈值应裁剪低分候选且不清空结果"
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "True", "默认装配 rerank 恒开应走校准路径"
    assert threshold.detail["min_score_ratio"] == "0.6", "显式配置的相对阈值应生效"
    assert threshold.detail["dropped"] == "1", "低于比例线的候选应被裁剪"


def test_default_config_threshold_off_end_to_end() -> None:
    # 出厂默认 min_score_ratio=0.0：相对阈值不裁剪，弱相关候选保留交由调用方 top_k
    # 决定。相对阈值会随融合分布变化误杀尾部候选（分层召回下尤甚——有无 layers
    # 属索引覆盖差异而非相关性差异），故默认关闭，需要时按场景显式开启。
    raw = default_config_dict()
    raw["globals"]["graph_enabled"] = False
    ctx = AssemblyContext.from_dict(raw)

    register_plugins()
    register_backends()
    register_operators()
    Factory.reset_all()
    try:
        retriever = RetrieverProducer.build_named("default", ctx)
        stack = SimpleNamespace(
            kv=KvProducer.build_named("default", ctx),
            vector=VectorProducer.build_named("default", ctx),
            fulltext=FulltextProducer.build_named("default", ctx),
            embedder=EmbedderProducer.build_named("default", ctx),
        )
        index_unit(stack, make_unit("u1", "alice likes coffee"))
        index_unit(stack, make_unit("u2", "bob drinks coffee"))

        result = retriever.retrieve(
            DEFAULT_SCOPE,
            RetrievalQuery(text="alice coffee", top_k=10, with_trajectory=True),
        )
    finally:
        Factory.reset_all()

    assert [item.unit_id for item in result.items] == ["u1", "u2"], "默认不应按比例裁剪"
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["min_score_ratio"] == "0", "出厂默认相对阈值应为 0"
    assert threshold.detail["dropped"] == "0"


def test_rerank_requested_without_reranker_records_skip() -> None:
    # 显式要求精排但装配未注入 reranker：降级须在轨迹可见，阈值走未校准路径。
    world = make_world()
    index_unit(world, make_unit("u1", "coffee beans"))
    retriever = PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        None,
        domain_store=_storage_for(world, [world.keyword]),
    )

    result = retriever.retrieve(
        DEFAULT_SCOPE, RetrievalQuery(text="coffee", rerank=True, with_trajectory=True)
    )

    rerank_steps = [step for step in result.trajectory if step.stage == "rerank"]
    assert rerank_steps, "请求精排但无 reranker 时应记录 rerank 轨迹步"
    assert rerank_steps[0].detail["skipped"] == "no_reranker_configured"
    threshold = [step for step in result.trajectory if step.stage == "threshold"][0]
    assert threshold.detail["calibrated"] == "False", "无 reranker 时阈值应走未校准路径"


def test_recall_max_below_floor_warns(monkeypatch) -> None:
    # recall_max 压过 over_fetch_floor 属于矛盾配置：上限赢，但装配期必须告警可见。
    # 直接桩掉模块 logger.warning——不依赖全局日志传播配置（setup_logging 会关 propagate）。
    import jiuwen_memory.retrieval.retriever_impl.pipeline_retriever as pr_module

    warnings: list[str] = []
    monkeypatch.setattr(
        pr_module.logger, "warning", lambda msg, *args: warnings.append(msg % args)
    )
    world = make_world()

    PipelineRetriever(
        world.parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        over_fetch_floor=60,
        recall_max=30,
        # 构造期告警测试：不触发召回，数据面给最小可用实例即可。
        domain_store=CompositeDomainStore(
            manager=CompositeStoreManager(),
            preferred_pipeline=RetrievalPipeline.RECALL_GET_RANK,
        ),
    )

    assert warnings, "recall_max < over_fetch_floor 应产生装配期告警"
    assert "recall_max" in warnings[0], "告警文案应指明 recall_max 压过下限"


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
        RRFFuser(),
        StructuredDiscloser(),
        world.unit_reader,
        domain_store=_storage_for(
            world,
            [
                StaticRecaller(
                    [
                        ScoredUnit("u1", 1.0, RecallChannel.KEYWORD),
                        ScoredUnit("u2", 1.0, RecallChannel.KEYWORD),
                    ]
                )
            ],
        ),
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
        WeightedRRFFuser(
            k=0,
            channel_weights={RecallChannel.KEYWORD: 2.0, RecallChannel.VECTOR: 1.0},
        ),
        StructuredDiscloser(),
        world.unit_reader,
        domain_store=_storage_for(
            world,
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
        ),
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
    top = result.items[0]
    # 三层都填：abstract(L0) 含 [summary]/[why]，overview(L1) 含 [evidence]
    assert "[summary] packages/foo package manager is pnpm." in top.abstract
    assert "[why] keyword(rank=1,score=0.3,weight=2,contribution=2)" in top.abstract
    assert "[evidence] packages/foo package manager is pnpm." in top.overview


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


def test_default_config_attaches_l0_l1_recallers() -> None:
    """默认 config（无 globals.layers_index_enabled）下，pipeline 默认接入 L0/L1 recaller。

    回归 #5：构建侧默认 true、召回侧原默认 false → 默认态下分层索引被建却不被查。
    对齐后召回侧默认 true，默认 config 应挂上 keyword/vector 的 l0/l1 recaller，
    且 defaults 已配 layers_l0/l1 具名 store → recaller 拿到非 None store。
    """
    raw = default_config_dict()
    # 不设 globals.layers_index_enabled —— 验证默认值对齐（应默认 true）
    raw["globals"].pop("layers_index_enabled", None)
    ctx = AssemblyContext.from_dict(raw)

    register_plugins()
    register_backends()
    register_operators()
    Factory.reset_all()
    try:
        retriever = RetrieverProducer.build_named("default", ctx)
        assert isinstance(retriever, PipelineRetriever)
        storage = retriever.storage
        assert isinstance(storage, CompositeDomainStore)

        # (channel, layer) 联合 key——keyword/vector 各 l0/l1，共四路
        by_key = {
            (r.channel().value, r.layer): r
            for r in storage.recallers
            if isinstance(r, (KeywordRecaller, VectorRecaller))
        }
        # 默认接入四路分层 recaller（keyword+vector 各 l0/l1）
        assert ("keyword", "l0") in by_key
        assert ("keyword", "l1") in by_key
        assert ("vector", "l0") in by_key
        assert ("vector", "l1") in by_key

        def _store_of(r):
            # KeywordRecaller.fulltext_store / VectorRecaller.vector_store
            return getattr(r, "fulltext_store", None) or getattr(r, "vector_store", None)

        # defaults 已配 layers_l0/l1 具名 store → store 非空（非降级）
        for key, r in by_key.items():
            assert _store_of(r) is not None, f"{key} recaller 未注入分层 store"
    finally:
        Factory.reset_all()
