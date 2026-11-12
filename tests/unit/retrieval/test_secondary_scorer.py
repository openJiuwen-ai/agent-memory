"""SecondaryScorer（retrieval F09 次级排序）单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, ScoredMemoryUnit, Temporal
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_config_dict
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.retrieval.fuser_impl.secondary_scorer import (
    SecondaryScorer,
    SecondaryScorerProducer,
)
from jiuwen_memory.retrieval.fuser_impl.secondary_scorer.default_secondary_scorer import (
    DefaultSecondaryScorer,
    parse_importance_value,
    recency_value,
)
from jiuwen_memory.retrieval.retriever import RetrieverProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.bootstrap import register_backends

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
_MISSING = object()


def _cand(
    uid: str,
    score: float,
    *,
    importance: object = _MISSING,
    age_days: float | None = 0.0,
) -> ScoredMemoryUnit:
    system_metadata = {} if importance is _MISSING else {"importance": importance}
    t_ingest = None if age_days is None else NOW - timedelta(days=age_days)
    unit = MemoryUnit(
        id=uid, system_metadata=system_metadata, temporal=Temporal(t_ingest=t_ingest)
    )
    return ScoredMemoryUnit(unit, score, RecallChannel.KEYWORD)


def _scorer(**kwargs) -> DefaultSecondaryScorer:
    return DefaultSecondaryScorer(clock=lambda: NOW, **kwargs)


def _ids(candidates) -> list[str]:
    return [candidate.unit_id for candidate in candidates]


def _rank(scorer: SecondaryScorer, candidates, *, calibrated: bool = True):
    return scorer.score(ParsedQuery(), candidates, calibrated=calibrated)


def test_disabled_by_default_no_reorder() -> None:
    assert default_config_dict()["globals"]["secondary_rank_enabled"] is False, (
        "出厂默认须关闭次级排序"
    )


@pytest.mark.parametrize(
    ("in_globals", "in_params", "expected"),
    [
        (None, None, False),  # 两处都不写：出厂关闭
        (True, None, True),  # 只写 globals：生效
        (None, True, True),  # 只写 retriever params：生效
        (True, False, False),  # 两处都写：本实例 params 优先
    ],
)
def test_switch_read_from_params_or_globals(in_globals, in_params, expected) -> None:
    # pylint: disable=protected-access
    register_plugins()
    register_backends()
    register_operators()
    raw = default_config_dict()
    if in_globals is not None:
        raw["globals"]["secondary_rank_enabled"] = in_globals
    if in_params is not None:
        raw["retriever"]["default"]["params"]["secondary_rank_enabled"] = in_params
    try:
        retriever = RetrieverProducer.build_named("default", AssemblyContext.from_dict(raw))
        assert retriever._secondary_rank_enabled is expected
        assert (retriever._secondary_scorer is not None) is expected
    finally:
        Factory.reset_all()


def test_operator_type_is_secondary_scorer() -> None:
    assert _scorer().operator_type() is RetrievalOperatorType.SECONDARY_SCORER


def test_single_candidate_skipped() -> None:
    only = [_cand("a", 0.9, importance="0.1")]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), only, calibrated=True)
    assert _ids(ranked) == ["a"]
    assert detail == {"skipped": "single_candidate"}


def test_no_candidates_skipped_as_empty() -> None:
    """阈值砍光候选时编排层仍会调用算子（不判空），轨迹须区分 0 条与 1 条。"""
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), [], calibrated=True)
    assert ranked == []
    assert detail == {"skipped": "empty"}, "空候选不得复用 single_candidate 标签"


def test_uncalibrated_path_skipped() -> None:
    candidates = [_cand("a", 0.02, importance="0.1"), _cand("b", 0.019, importance="0.9")]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), candidates, calibrated=False)
    assert _ids(ranked) == ["a", "b"], "未精排路径不得重排"
    assert detail == {"skipped": "uncalibrated"}


def test_band_split_on_gap_exceeding_epsilon() -> None:
    # a 与 b 相差 0.10 > ε：分两带，b 重要度再高也不能越过 a。
    candidates = [_cand("a", 0.90, importance="0.1"), _cand("b", 0.80, importance="1.0")]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), candidates, calibrated=True)
    assert _ids(ranked) == ["a", "b"]
    assert detail["band_count"] == "2"
    assert detail["reordered"] == "0"


def test_band_merge_within_epsilon() -> None:
    candidates = [_cand("a", 0.90, importance="0.1"), _cand("b", 0.87, importance="0.9")]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), candidates, calibrated=True)
    assert _ids(ranked) == ["b", "a"], "带内应按次要键重排"
    assert detail["band_count"] == "1"
    assert detail["max_band_size"] == "2"
    assert detail["reordered"] == "2"


def test_band_anchored_to_band_start_not_chained() -> None:
    # 0.90 → 0.86 → 0.82：相邻落差都 ≤ ε，但 0.82 距带首 0.08 > ε，须开新带（带宽恒 ≤ ε）。
    candidates = [
        _cand("a", 0.90, importance="0.1"),
        _cand("b", 0.86, importance="0.5"),
        _cand("c", 0.82, importance="1.0"),
    ]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), candidates, calibrated=True)
    assert _ids(ranked) == ["b", "a", "c"]
    assert detail["band_count"] == "2"


def test_stable_sort_on_equal_secondary_score() -> None:
    candidates = [
        _cand("a", 0.90, importance="0.5"),
        _cand("b", 0.89, importance="0.5"),
        _cand("c", 0.88, importance="0.5"),
    ]
    assert _ids(_rank(_scorer(), candidates)) == ["a", "b", "c"]


def test_high_importance_old_vs_low_importance_new() -> None:
    old_important = _cand("old", 0.90, importance="0.95", age_days=365)  # 0.475+0.0377
    new_trivial = _cand("new", 0.89, importance="0.30", age_days=0)  # 0.15+0.5
    assert _ids(_rank(_scorer(), [old_important, new_trivial])) == ["new", "old"]
    # 权重偏重要度后翻转。
    tilted = _scorer(importance_weight=0.9, recency_weight=0.1)
    assert _ids(_rank(tilted, [old_important, new_trivial])) == ["old", "new"]


def test_recency_zero_on_missing_t_ingest() -> None:
    assert recency_value(None, NOW, 30.0) == 0.0
    # 无 t_ingest 时仅靠重要度：同重要度下有 t_ingest 者靠前。
    candidates = [
        _cand("no_time", 0.90, importance="0.5", age_days=None),
        _cand("timed", 0.89, importance="0.5", age_days=30),
    ]
    assert _ids(_rank(_scorer(), candidates)) == ["timed", "no_time"]


def test_recency_clamped_on_future_t_ingest() -> None:
    assert recency_value(NOW + timedelta(days=3), NOW, 30.0) == 1.0


def test_recency_treats_naive_t_ingest_as_utc() -> None:
    naive = (NOW - timedelta(days=30)).replace(tzinfo=None)
    assert recency_value(naive, NOW, 30.0) == pytest.approx(0.5)


def test_importance_fallback_on_missing() -> None:
    assert parse_importance_value(None) == pytest.approx(0.5)


def test_importance_fallback_on_non_numeric() -> None:
    assert parse_importance_value("high") == pytest.approx(0.5)
    assert parse_importance_value("nan") == pytest.approx(0.5)
    assert parse_importance_value(True) == pytest.approx(0.5)


def test_importance_parsed_numerically_not_lexicographically() -> None:
    # 字符串字典序下 "0.5" > "0.45" 成立，但跨长度比较会失真；须按数值解析。
    assert parse_importance_value("0.5") < parse_importance_value("0.95")
    assert parse_importance_value("1") == pytest.approx(1.0)


def test_importance_clamped_out_of_range() -> None:
    assert parse_importance_value("1.7") == pytest.approx(1.0)
    assert parse_importance_value("-0.2") == pytest.approx(0.0)


def test_recency_decay_curve() -> None:
    assert recency_value(NOW, NOW, 30.0) == pytest.approx(1.0)
    assert recency_value(NOW - timedelta(days=30), NOW, 30.0) == pytest.approx(0.5)
    assert recency_value(NOW - timedelta(days=60), NOW, 30.0) == pytest.approx(1 / 3)


def test_all_in_one_band_degrades_to_secondary_sort() -> None:
    candidates = [
        _cand("a", 0.90, importance="0.1"),
        _cand("b", 0.89, importance="0.9"),
        _cand("c", 0.88, importance="0.5"),
    ]
    ranked, detail = _scorer().score_with_detail(ParsedQuery(), candidates, calibrated=True)
    assert _ids(ranked) == ["b", "c", "a"]
    assert detail["band_count"] == "1"


def test_band_boundary_inclusive() -> None:
    # 0.90 - 0.05 在二进制下略大于 0.85；边界按闭区间，0.85 仍归当前带。
    candidates = [_cand("a", 0.90, importance="0.1"), _cand("b", 0.85, importance="0.9")]
    assert _ids(_rank(_scorer(), candidates)) == ["b", "a"]


def test_weights_not_required_to_sum_one() -> None:
    candidates = [
        _cand("a", 0.90, importance="0.2", age_days=0),
        _cand("b", 0.89, importance="0.9", age_days=200),
        _cand("c", 0.88, importance="0.6", age_days=10),
    ]
    small = _rank(_scorer(importance_weight=0.7, recency_weight=0.3), candidates)
    large = _rank(_scorer(importance_weight=1.4, recency_weight=0.6), candidates)
    assert _ids(small) == _ids(large)


def test_unmaterialized_candidate_uses_fallback_importance() -> None:
    bare = ScoredUnit("bare", 0.90, RecallChannel.KEYWORD)
    important = _cand("important", 0.89, importance="0.9", age_days=None)
    assert _ids(_rank(_scorer(), [bare, important])) == ["important", "bare"]


def test_invalid_params_rejected() -> None:
    with pytest.raises(ValidationError):
        DefaultSecondaryScorer(recency_tau_days=0)
    with pytest.raises(ValidationError):
        DefaultSecondaryScorer(score_band=-0.1)


class _ReverseScorer(SecondaryScorer):
    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.SECONDARY_SCORER

    def health(self) -> None:
        return None

    def explain(self) -> dict[str, str]:
        return {"strategy": "reverse"}

    def score(self, query, candidates, *, calibrated):
        return list(reversed(candidates))


def test_custom_scorer_target_swaps_implementation() -> None:
    register_operators()
    with SecondaryScorerProducer.registration_scope():
        SecondaryScorerProducer.register("reverse_test")(lambda _config: _ReverseScorer())
        raw = default_config_dict()
        raw["secondary_scorer"]["default"]["target"] = "reverse_test"
        ctx = AssemblyContext.from_dict(raw)
        scorer = SecondaryScorerProducer.build_named("default", ctx)
    assert isinstance(scorer, _ReverseScorer)
    # 只实现 score 的业务实现，score_with_detail 明细为空。
    ranked, detail = scorer.score_with_detail(
        ParsedQuery(), [_cand("a", 0.9), _cand("b", 0.8)], calibrated=True
    )
    assert _ids(ranked) == ["b", "a"]
    assert detail == {}


def test_default_target_reads_params_from_config() -> None:
    register_operators()
    raw = default_config_dict()
    raw["secondary_scorer"]["default"]["params"]["score_band"] = 0.03
    raw["secondary_scorer"]["default"]["params"]["recency_tau_days"] = 14
    scorer = SecondaryScorerProducer.build_named("default", AssemblyContext.from_dict(raw))
    assert isinstance(scorer, DefaultSecondaryScorer)
    assert scorer.explain()["score_band"] == "0.03"
    assert scorer.explain()["recency_tau_days"] == "14"
