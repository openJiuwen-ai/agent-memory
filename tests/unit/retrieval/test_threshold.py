"""PipelineRetriever threshold stage tests."""

from __future__ import annotations

import pytest

from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import apply_threshold
from jiuwen_memory.retrieval.types import RecallChannel, ScoredUnit

pytestmark = pytest.mark.unit


def _units(scores: list[float]) -> list[ScoredUnit]:
    return [
        ScoredUnit(f"u{i}", score, RecallChannel.KEYWORD)
        for i, score in enumerate(scores, start=1)
    ]


def _ids(units: list[ScoredUnit]) -> list[str]:
    return [unit.unit_id for unit in units]


def test_absolute_threshold_drops_below() -> None:
    kept, detail = apply_threshold(
        _units([0.9, 0.5, 0.49]), 10, calibrated=True, min_score=0.5
    )

    assert _ids(kept) == ["u1", "u2"], "校准路径应应用绝对阈值"
    assert detail["passed"] == "2"
    assert detail["dropped"] == "1"


def test_relative_threshold_keeps_ratio_of_max() -> None:
    kept, detail = apply_threshold(
        _units([0.8, 0.4, 0.39]),
        10,
        calibrated=False,
        min_score_ratio_uncalibrated=0.5,
    )

    assert _ids(kept) == ["u1", "u2"], "相对阈值应按最高分比例裁剪"
    assert detail["calibrated"] == "False"
    assert detail["min_score_ratio"] == "0.5"


def test_ratio_default_selected_by_calibration() -> None:
    # 同一组候选，校准/未校准路径分别取严(0.6)/松(0.3)两套相对阈值默认。
    units = _units([1.0, 0.5, 0.2])  # 相对最高分 1.0：0.6 线卡 0.6，0.3 线卡 0.3

    calibrated, cal_detail = apply_threshold(
        units,
        10,
        calibrated=True,
        min_score_ratio=0.6,
        min_score_ratio_uncalibrated=0.3,
    )
    uncalibrated, unc_detail = apply_threshold(
        units,
        10,
        calibrated=False,
        min_score_ratio=0.6,
        min_score_ratio_uncalibrated=0.3,
    )

    assert _ids(calibrated) == ["u1"], "校准路径用 0.6：0.5/0.2 均被砍"
    assert cal_detail["min_score_ratio"] == "0.6"
    assert _ids(uncalibrated) == ["u1", "u2"], "未校准路径用 0.3：0.5 保留、0.2 砍"
    assert unc_detail["min_score_ratio"] == "0.3"


def test_both_off_keeps_all_positive() -> None:
    kept, detail = apply_threshold(_units([0.2, 0.0, -0.1, 0.1]), 10, calibrated=True)

    assert _ids(kept) == ["u1", "u4"], "默认仅保留正分候选"
    assert detail["positive"] == "2"


def test_positive_gate_applies_uncalibrated_path() -> None:
    # 钉住语义：正分门在未精排路径同样生效——零证据候选（如 weighted_rrf
    # 零权重通道产出的 0 分、自定义 fuser 的负分）不进入结果。
    kept, detail = apply_threshold(_units([0.03, 0.0, -0.5]), 10, calibrated=False)

    assert _ids(kept) == ["u1"], "未校准路径零/负融合分候选应被正分门丢弃"
    assert detail["positive"] == "1"
    assert detail["dropped"] == "2"


def test_all_below_returns_empty() -> None:
    kept, detail = apply_threshold(_units([0.4, 0.3]), 10, calibrated=True, min_score=0.5)

    assert kept == [], "无候选过阈值且无兜底时应欠填为空"
    assert detail["out"] == "0"


def test_min_results_backfills() -> None:
    kept, detail = apply_threshold(
        _units([0.9, 0.7, 0.6, 0.0]),
        5,
        calibrated=True,
        min_score=0.8,
        min_results=3,
    )

    assert _ids(kept) == ["u1", "u2", "u3"], "min_results 应从正分候选中回填"
    assert detail["passed"] == "1"
    assert detail["backfilled"] == "2"


def test_min_results_clamped_to_top_k() -> None:
    kept, detail = apply_threshold(
        _units([0.8, 0.7, 0.6, 0.5]),
        2,
        calibrated=True,
        min_score=0.9,
        min_results=20,
    )

    assert _ids(kept) == ["u1", "u2"], "min_results 不能突破调用级 top_k"
    assert detail["out"] == "2"


def test_empty_input_returns_empty() -> None:
    kept, detail = apply_threshold([], 5, calibrated=True)

    assert kept == [], "空候选应稳定返回空结果"
    assert detail["dropped"] == "0"


def test_absolute_skipped_when_not_calibrated() -> None:
    uncalibrated, uncalibrated_detail = apply_threshold(
        _units([0.02, 0.01]), 5, calibrated=False, min_score=0.4
    )
    calibrated, calibrated_detail = apply_threshold(
        _units([0.02, 0.01]), 5, calibrated=True, min_score=0.4
    )

    assert _ids(uncalibrated) == ["u1", "u2"], "未精排路径不应套用 rerank 量纲绝对阈值"
    assert uncalibrated_detail["calibrated"] == "False"
    assert calibrated == [], "校准路径应应用同一个绝对阈值"
    assert calibrated_detail["calibrated"] == "True"
