"""Fuser tests."""

from __future__ import annotations

import pytest

from config import AssemblyContext
from retrieval.fuser_impl import FuserProducer
from retrieval.fuser_impl.rrf_fuser import RRFFuser
from retrieval.fuser_impl.weighted_rrf_fuser import WeightedRRFFuser
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit

pytestmark = pytest.mark.unit


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
