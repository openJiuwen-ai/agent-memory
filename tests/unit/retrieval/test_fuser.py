"""Fuser tests."""

from __future__ import annotations

import pytest

from common.factory.factory import Factory
from config import AssemblyContext
from config.defaults import default_context
from retrieval.fuser_impl import FuserProducer
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.fuser_impl.score_max_fuser import ScoreMaxFuser
from retrieval.fuser_impl.weighted_rrf_fuser import WeightedRRFFuser
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit

pytestmark = pytest.mark.unit


def test_default_config_uses_rrf_fuser() -> None:
    """内置出厂默认保持 RRF，score_max 仅在显式配置时启用。"""
    Factory.reset_all()
    try:
        fuser = FuserProducer.build_named("default", default_context())
    finally:
        Factory.reset_all()

    assert isinstance(fuser, RRFFuser)


def test_rrf_fuser_dedups_and_ranks_shared_hits_first() -> None:
    keyword_results = [
        ScoredUnit("u1", 0.9, RecallChannel.KEYWORD),
        ScoredUnit("u2", 0.5, RecallChannel.KEYWORD),
    ]
    vector_results = [
        ScoredUnit("u2", 0.8, RecallChannel.VECTOR),
        ScoredUnit("u3", 0.4, RecallChannel.VECTOR),
    ]

    fused = RRFFuser().fuse(ParsedQuery(), [keyword_results, vector_results])
    ids = [scored.unit_id for scored in fused]

    assert set(ids) == {"u1", "u2", "u3"}
    assert ids[0] == "u2"


def test_rrf_fuser_records_channel_evidence() -> None:
    keyword_results = [
        ScoredUnit("u1", 0.9, RecallChannel.KEYWORD),
        ScoredUnit("u2", 0.5, RecallChannel.KEYWORD),
    ]
    vector_results = [ScoredUnit("u2", 0.8, RecallChannel.VECTOR)]

    fused = RRFFuser(k=60).fuse(ParsedQuery(), [keyword_results, vector_results])
    shared = next(scored for scored in fused if scored.unit_id == "u2")

    assert [(item.channel, item.rank, item.score) for item in shared.evidence] == [
        (RecallChannel.KEYWORD, 1, 0.5),
        (RecallChannel.VECTOR, 0, 0.8),
    ]
    assert shared.evidence[0].contribution == pytest.approx(1.0 / 62.0)
    assert shared.evidence[1].contribution == pytest.approx(1.0 / 61.0)


def test_weighted_rrf_fuser_applies_channel_weights() -> None:
    keyword_results = [ScoredUnit("keyword_hit", 0.6, RecallChannel.KEYWORD)]
    vector_results = [ScoredUnit("vector_hit", 0.9, RecallChannel.VECTOR)]
    fuser = WeightedRRFFuser(
        k=0,
        channel_weights={RecallChannel.KEYWORD: 2.0, RecallChannel.VECTOR: 1.0},
    )

    fused = fuser.fuse(ParsedQuery(), [keyword_results, vector_results])

    assert [scored.unit_id for scored in fused] == ["keyword_hit", "vector_hit"]
    assert fused[0].score == pytest.approx(2.0)
    assert fused[1].score == pytest.approx(1.0)
    assert fused[0].evidence[0].weight == pytest.approx(2.0)
    assert fused[0].evidence[0].contribution == pytest.approx(2.0)


def test_weighted_rrf_fuser_can_be_created_from_config() -> None:
    fuser = FuserProducer.build(
        "weighted_rrf",
        {"fusion_rrf_k": 10, "fusion_channel_weights": {"keyword": "2.5"}},
        AssemblyContext(),
    )

    fused = fuser.fuse(
        ParsedQuery(),
        [[ScoredUnit("u1", 0.9, RecallChannel.KEYWORD)]],
    )

    assert isinstance(fuser, WeightedRRFFuser)
    assert fused[0].score == pytest.approx(2.5 / 11.0)
    assert fuser.explain() == {
        "strategy": "weighted_rrf",
        "rrf_k": "10",
        "channel_weights": "keyword=2.5",
    }


@pytest.mark.parametrize("fuser", [RRFFuser(k=0), WeightedRRFFuser(k=0)])
def test_rrf_family_merges_same_channel_layers_before_scoring(fuser) -> None:
    """同通道 L2/L0 是分层索引入口，同 unit 不能因多层命中重复投票。"""
    l2 = [
        ScoredUnit("u1", 0.9, RecallChannel.VECTOR),
        ScoredUnit("u2", 0.8, RecallChannel.VECTOR),
    ]
    l0 = [ScoredUnit("u1", 0.7, RecallChannel.VECTOR)]

    fused = fuser.fuse(ParsedQuery(), [l2, l0])
    u1 = next(su for su in fused if su.unit_id == "u1")

    assert len(u1.evidence) == 1, "u1 的多层命中应先 MaxP 归并再计分"
    assert u1.evidence[0].score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# ScoreMaxFuser：通道内 max 归一化 + 通道间取最大值
# ---------------------------------------------------------------------------


def test_score_max_normalizes_within_channel_by_top_score() -> None:
    """各通道以自身最高分为 1.0 基准，消除 BM25 与余弦的量纲差异。"""
    keyword_results = [
        ScoredUnit("u_kw_top", 20.0, RecallChannel.KEYWORD),
        ScoredUnit("u_kw_mid", 10.0, RecallChannel.KEYWORD),
    ]
    vector_results = [ScoredUnit("u_vec", 0.8, RecallChannel.VECTOR)]

    fused = ScoreMaxFuser().fuse(ParsedQuery(), [keyword_results, vector_results])
    by_id = {su.unit_id: su for su in fused}

    assert by_id["u_kw_top"].score == pytest.approx(1.0)  # 20/20
    assert by_id["u_kw_mid"].score == pytest.approx(0.5)  # 10/20
    assert by_id["u_vec"].score == pytest.approx(1.0)  # 0.8/0.8


def test_score_max_does_not_penalize_single_channel_candidate() -> None:
    """语义极强但字面未命中的候选不被折价——这是相对加法/RRF 的核心差异。"""
    keyword_results = [
        ScoredUnit("u_both", 20.0, RecallChannel.KEYWORD),
        ScoredUnit("u_weak_both", 9.0, RecallChannel.KEYWORD),
    ]
    vector_results = [
        ScoredUnit("u_both", 0.735, RecallChannel.VECTOR),
        ScoredUnit("u_vec_only", 0.711, RecallChannel.VECTOR),  # 语义第二强，keyword 未命中
        ScoredUnit("u_weak_both", 0.660, RecallChannel.VECTOR),
    ]

    fused = ScoreMaxFuser().fuse(ParsedQuery(), [keyword_results, vector_results])
    order = [su.unit_id for su in fused]

    # 加法融合下 u_vec_only 得 (0.711+0)/2=0.356，会被双路命中的 u_weak_both
    # (0.5+0.66)/2 反超；取最大值后按各自最佳相对强度排序。
    assert order.index("u_vec_only") < order.index("u_weak_both")
    assert fused[0].unit_id == "u_both"


def test_score_max_takes_best_channel_not_sum() -> None:
    """双通道命中取较高者，不累加——多命中一路不构成加分。"""
    keyword_results = [ScoredUnit("u1", 10.0, RecallChannel.KEYWORD)]
    vector_results = [ScoredUnit("u1", 0.5, RecallChannel.VECTOR)]

    fused = ScoreMaxFuser().fuse(ParsedQuery(), [keyword_results, vector_results])

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(1.0), "两路均为各自最高分，取 max 而非求和"
    assert len(fused[0].evidence) == 2, "证据仍记录全部通道贡献"


def test_score_max_merges_layers_before_normalizing() -> None:
    """分层必须先归并再归一化：否则候选少的层会把弱命中抬到与主层最强同级。"""
    l2 = [
        ScoredUnit("u_strong", 20.0, RecallChannel.KEYWORD),
        ScoredUnit("u_mid", 10.0, RecallChannel.KEYWORD),
    ]
    l0 = [ScoredUnit("u_weak", 3.0, RecallChannel.KEYWORD)]  # 该层仅一条，最高分很低

    fused = ScoreMaxFuser().fuse(ParsedQuery(), [l2, l0])
    by_id = {su.unit_id: su for su in fused}

    # 若按层各自归一化，u_weak 会得 3/3 = 1.0 与 u_strong 并列
    assert by_id["u_weak"].score == pytest.approx(3.0 / 20.0)
    assert by_id["u_strong"].score == pytest.approx(1.0)
    assert fused[0].unit_id == "u_strong"


def test_score_max_ignores_layer_multiplicity() -> None:
    """同 unit 多层命中不产生额外增益（取最高分）。"""
    fuser = ScoreMaxFuser()
    l2 = [ScoredUnit("u1", 0.6, RecallChannel.VECTOR), ScoredUnit("u2", 0.5, RecallChannel.VECTOR)]
    l0 = [ScoredUnit("u1", 0.55, RecallChannel.VECTOR)]

    without_layer = fuser.fuse(ParsedQuery(), [l2])
    with_layer = fuser.fuse(ParsedQuery(), [l2, l0])

    assert [su.unit_id for su in without_layer] == [su.unit_id for su in with_layer]
    assert with_layer[0].score == pytest.approx(without_layer[0].score)


def test_score_max_handles_non_positive_channel() -> None:
    """整路无有效信号（最高分非正）时该路计 0，不除零。"""
    keyword_results = [ScoredUnit("u1", 0.0, RecallChannel.KEYWORD)]
    vector_results = [ScoredUnit("u1", 0.5, RecallChannel.VECTOR)]

    fused = ScoreMaxFuser().fuse(ParsedQuery(), [keyword_results, vector_results])

    assert fused[0].score == pytest.approx(1.0)  # 取 vector 归一化后的 1.0


def test_score_max_can_be_created_from_config() -> None:
    fuser = FuserProducer.build(
        "score_max",
        {"fusion_channel_weights": {"keyword": "0.5"}},
        AssemblyContext(),
    )

    assert isinstance(fuser, ScoreMaxFuser)
    assert fuser.explain() == {
        "strategy": "score_max",
        "normalization": "channel_max",
        "channel_weights": "keyword=0.5",
    }

    # 权重压制 keyword：其归一化 1.0 被折为 0.5，低于 vector 的 1.0
    fused = fuser.fuse(
        ParsedQuery(),
        [
            [ScoredUnit("u_kw", 20.0, RecallChannel.KEYWORD)],
            [ScoredUnit("u_vec", 0.6, RecallChannel.VECTOR)],
        ],
    )
    assert [su.unit_id for su in fused] == ["u_vec", "u_kw"]
