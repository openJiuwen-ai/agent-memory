"""BM25 粗排算子 tests."""

from __future__ import annotations

import pytest

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.tokenizer.tokenizer_impl import TokenizerProducer
from jiuwen_memory.common.type_def import MemoryUnit, ScoredMemoryUnit, Segment
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.retrieval.fuser_impl import FuserProducer
from jiuwen_memory.retrieval.fuser_impl.bm25_scored_fuser import BM25ScoredFuser
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.fuser_impl.score_max_fuser import ScoreMaxFuser
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit

pytestmark = pytest.mark.unit

QUERY = ParsedQuery(raw="quarterly report", tokens=["quarterly", "report"])


@pytest.fixture
def tokenizer():
    return TokenizerProducer.build("whitespace", {}, default_context())


def _scored(unit_id: str, text: str, score: float, channel: RecallChannel):
    return ScoredMemoryUnit(
        MemoryUnit(id=unit_id, segments=[Segment(content=text)]), score, channel
    )


def test_default_config_still_uses_rrf() -> None:
    """出厂默认不变：bm25 仅在显式配置时启用。"""
    Factory.reset_all()
    try:
        assert isinstance(FuserProducer.build_named("default", default_context()), RRFFuser)
    finally:
        Factory.reset_all()


def test_registered_and_params_read_from_config() -> None:
    Factory.reset_all()
    try:
        fuser = FuserProducer.build(
            "BM25_scored_fusor",
            {"tokenizer": "default", "bm25_k1": 1.5, "bm25_b": 0.4},
            default_context(),
        )
    finally:
        Factory.reset_all()
    assert isinstance(fuser, BM25ScoredFuser)
    assert fuser.explain()["k1"] == "1.5"
    assert fuser.explain()["b"] == "0.4"


def test_lexical_axis_promotes_vector_only_candidate(tokenizer) -> None:
    """核心用例：向量召回的候选从未被 ES 词法打过分，本算子在并集上补齐。"""
    keyword = [_scored("u4", "quarterly revenue report annual", 5.0, RecallChannel.KEYWORD)]
    vector = [
        _scored("u1", "unrelated filler text here", 1.0, RecallChannel.VECTOR),
        _scored("u3", "another unrelated passage", 0.6, RecallChannel.VECTOR),
        _scored("u2", "quarterly report quarterly report", 0.4, RecallChannel.VECTOR),
    ]

    plain = [su.unit_id for su in ScoreMaxFuser().fuse(QUERY, [keyword, vector])]
    with_bm25 = [su.unit_id for su in BM25ScoredFuser(tokenizer).fuse(QUERY, [keyword, vector])]

    # u2 词法极强但向量分最低：未补词法轴时垫底，补齐后越过纯噪声的 u3。
    assert plain.index("u2") > plain.index("u3")
    assert with_bm25.index("u2") < with_bm25.index("u3")


def test_channel_scores_are_never_lowered(tokenizer) -> None:
    """只增不减：每个候选的融合分不低于纯 score_max 下的分。"""
    keyword = [
        _scored("u1", "quarterly revenue report", 3.0, RecallChannel.KEYWORD),
        _scored("u2", "the the the report the", 1.0, RecallChannel.KEYWORD),
    ]
    vector = [_scored("u3", "no lexical overlap here", 0.9, RecallChannel.VECTOR)]

    channels = [keyword, vector]
    plain = {su.unit_id: su.score for su in ScoreMaxFuser().fuse(QUERY, channels)}
    fused = {su.unit_id: su.score for su in BM25ScoredFuser(tokenizer).fuse(QUERY, channels)}

    assert set(plain) == set(fused)
    for uid, score in plain.items():
        assert fused[uid] >= score - 1e-12


def test_idf_and_length_norm_beat_raw_overlap(tokenizer) -> None:
    """旧式词重叠给「重复通用词的长文档」高分；BM25 按 IDF 与长度归一压回。"""
    vector = [
        _scored("u1", "the the the the report the the the", 0.5, RecallChannel.VECTOR),
        _scored("u2", "quarterly revenue report", 0.5, RecallChannel.VECTOR),
    ]

    fused = {su.unit_id: su for su in BM25ScoredFuser(tokenizer).fuse(QUERY, [vector])}
    lexical = {
        uid: next(e.score for e in su.evidence if e.channel is RecallChannel.KEYWORD)
        for uid, su in fused.items()
    }

    assert lexical["u2"] > lexical["u1"]


def test_lexical_weight_zero_degrades_to_score_max(tokenizer) -> None:
    keyword = [_scored("u1", "quarterly revenue report", 3.0, RecallChannel.KEYWORD)]
    vector = [_scored("u2", "no lexical overlap here", 0.9, RecallChannel.VECTOR)]

    off = BM25ScoredFuser(tokenizer, lexical_weight=0.0).fuse(QUERY, [keyword, vector])
    plain = ScoreMaxFuser().fuse(QUERY, [keyword, vector])

    assert [(su.unit_id, su.score) for su in off] == [(su.unit_id, su.score) for su in plain]


def test_evidence_records_the_lexical_axis(tokenizer) -> None:
    keyword = [_scored("u1", "quarterly revenue report", 3.0, RecallChannel.KEYWORD)]

    fused = BM25ScoredFuser(tokenizer).fuse(QUERY, [keyword])

    assert len(fused[0].evidence) == 2  # 召回通道一条 + 词法轴一条


def test_unmaterialized_candidates_skip_the_axis_without_zeroing(tokenizer) -> None:
    """Fuser 收到未物化的 ScoredUnit 时无内容可打分，退化为通道归一化。"""
    keyword = [
        ScoredUnit("u1", 0.9, RecallChannel.KEYWORD),
        ScoredUnit("u2", 0.5, RecallChannel.KEYWORD),
    ]

    fused = BM25ScoredFuser(tokenizer).fuse(QUERY, [keyword])

    assert [su.unit_id for su in fused] == ["u1", "u2"]
    assert fused[0].score == pytest.approx(1.0)


def test_empty_query_tokens_skip_the_axis(tokenizer) -> None:
    keyword = [_scored("u1", "quarterly revenue report", 0.5, RecallChannel.KEYWORD)]

    fused = BM25ScoredFuser(tokenizer).fuse(ParsedQuery(), [keyword])

    assert [su.unit_id for su in fused] == ["u1"]


def test_no_lexical_hit_anywhere_skips_the_axis(tokenizer) -> None:
    vector = [_scored("u1", "nothing in common", 0.5, RecallChannel.VECTOR)]

    fused = BM25ScoredFuser(tokenizer).fuse(QUERY, [vector])

    assert len(fused[0].evidence) == 1
    assert fused[0].score == pytest.approx(1.0)


def test_empty_candidates(tokenizer) -> None:
    assert BM25ScoredFuser(tokenizer).fuse(QUERY, []) == []


def test_layered_channels_merged_before_scoring(tokenizer) -> None:
    """同通道分层多路先归并取 MaxP，不得被当作多个信号源。"""
    l2 = [_scored("u1", "quarterly revenue report", 0.5, RecallChannel.KEYWORD)]
    l0 = [_scored("u1", "quarterly revenue report", 0.9, RecallChannel.KEYWORD)]

    fused = BM25ScoredFuser(tokenizer).fuse(QUERY, [l2, l0])

    assert len(fused) == 1
    assert sum(1 for e in fused[0].evidence if e.channel is RecallChannel.KEYWORD) == 2


def test_channel_weight_suppresses_a_channel(tokenizer) -> None:
    keyword = [_scored("u1", "quarterly revenue report", 3.0, RecallChannel.KEYWORD)]
    vector = [_scored("u2", "no lexical overlap here", 0.9, RecallChannel.VECTOR)]

    fused = BM25ScoredFuser(tokenizer, channel_weights={"vector": 0.3}).fuse(
        QUERY, [keyword, vector]
    )

    assert fused[0].unit_id == "u1"
    assert {su.unit_id: su.score for su in fused}["u2"] == pytest.approx(0.3)
