# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""默认 SecondaryScorer：分带 tie-break + 重要度 / 时效性线性次要键（retrieval F09）。

算法（候选已按校准相关性分降序）：

1. **分带**：以带首分为基准，分数 ``>= 带首分 - score_band`` 的连续候选归入同一带，
   首个低于该线的候选开启新带——带宽恒 ≤ ``score_band``，带间严格保持相关性顺序；
2. **带内次要键**：``importance × importance_weight + recency × recency_weight``，
   带内按次要键降序稳定排序（同键保持原相关性顺序）；
3. ``importance`` 取 ``system_metadata["importance"]`` 经 ``float()`` 解析（绕开字符串
   字典序），缺失 / 非数值兜底 0.50、越界裁剪 [0, 1]；
4. ``recency = 1 / (1 + age_days / recency_tau_days)``，``age`` 基于 ``t_ingest``；
   缺失时 0.0（该维度不贡献），未来时间（时钟漂移）裁剪到 1.0。

只在校准（已精排）路径生效：未精排的 RRF 分量纲密集，绝对带宽会把全部候选并入一带。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import ScoredCandidate
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.types import ParsedQuery

from .secondary_scorer import SecondaryScorer, SecondaryScorerProducer

DEFAULT_SCORE_BAND = 0.05
DEFAULT_IMPORTANCE_WEIGHT = 0.5
DEFAULT_RECENCY_WEIGHT = 0.5
DEFAULT_RECENCY_TAU_DAYS = 30.0
# importance 缺失 / 非数值时的兜底值（与 construction 侧 DEFAULT_IMPORTANCE 同值）。
FALLBACK_IMPORTANCE = 0.5
# 分带边界比较的浮点容差：0.90 - 0.05 在二进制下略大于 0.85，不加容差会把恰在
# 边界上的候选误切到下一带（边界按闭区间定义）。
_BAND_EPSILON = 1e-9
_SECONDS_PER_DAY = 86400.0


def _now_utc() -> datetime:
    return datetime.now(UTC)


def parse_importance_value(raw: object) -> float:
    """把 ``system_metadata["importance"]`` 解析为 [0, 1] 浮点；缺失 / 非法兜底 0.50。"""
    if raw is None or isinstance(raw, bool):
        return FALLBACK_IMPORTANCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return FALLBACK_IMPORTANCE
    if not math.isfinite(value):
        return FALLBACK_IMPORTANCE
    return min(1.0, max(0.0, value))


def recency_value(t_ingest: datetime | None, now: datetime, tau_days: float) -> float:
    """软衰减时效性 ``1 / (1 + age_days / tau)``；缺失为 0.0，未来时间裁剪到 1.0。"""
    if t_ingest is None:
        return 0.0
    if t_ingest.tzinfo is None:
        t_ingest = t_ingest.replace(tzinfo=UTC)
    age_days = (now - t_ingest).total_seconds() / _SECONDS_PER_DAY
    if age_days <= 0.0:
        return 1.0
    return 1.0 / (1.0 + age_days / tau_days)


class DefaultSecondaryScorer(SecondaryScorer):
    """分带 tie-break + ``w_i·importance + w_r·recency`` 的默认次级排序。"""

    def __init__(
        self,
        *,
        score_band: float = DEFAULT_SCORE_BAND,
        importance_weight: float = DEFAULT_IMPORTANCE_WEIGHT,
        recency_weight: float = DEFAULT_RECENCY_WEIGHT,
        recency_tau_days: float = DEFAULT_RECENCY_TAU_DAYS,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._score_band = float(score_band)
        self._importance_weight = float(importance_weight)
        self._recency_weight = float(recency_weight)
        self._recency_tau_days = float(recency_tau_days)
        self._clock = clock
        for name, value in (
            ("score_band", self._score_band),
            ("importance_weight", self._importance_weight),
            ("recency_weight", self._recency_weight),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValidationError(f"secondary_scorer.{name} 须为非负有限数，得到 {value!r}")
        if not math.isfinite(self._recency_tau_days) or self._recency_tau_days <= 0.0:
            raise ValidationError(
                f"secondary_scorer.recency_tau_days 须为正数，得到 {self._recency_tau_days!r}"
            )

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.SECONDARY_SCORER

    def health(self) -> None:
        return None

    def explain(self) -> dict[str, str]:
        return {
            "strategy": "band_tiebreak",
            "score_band": f"{self._score_band:g}",
            "importance_weight": f"{self._importance_weight:g}",
            "recency_weight": f"{self._recency_weight:g}",
            "recency_tau_days": f"{self._recency_tau_days:g}",
        }

    def score(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        calibrated: bool,
    ) -> list[ScoredCandidate]:
        return self.score_with_detail(query, candidates, calibrated=calibrated)[0]

    def score_with_detail(
        self,
        query: ParsedQuery,
        candidates: list[ScoredCandidate],
        *,
        calibrated: bool,
    ) -> tuple[list[ScoredCandidate], dict[str, str]]:
        if not calibrated:
            return list(candidates), {"skipped": "uncalibrated"}
        # 空候选与单候选分开记：阈值把候选全砍掉时编排层仍会调用本算子
        # （pipeline_retriever 不判空），轨迹上应能区分 0 条与 1 条。
        if not candidates:
            return [], {"skipped": "empty"}
        if len(candidates) == 1:
            return list(candidates), {"skipped": "single_candidate"}
        now = self._clock()
        bands = self._split_bands(candidates)
        ordered: list[ScoredCandidate] = []
        for band in bands:
            if len(band) == 1:
                ordered.extend(band)
                continue
            # sorted 稳定：同次要键保持带内原相关性顺序。
            ordered.extend(sorted(band, key=lambda c: -self._secondary_key(c, now)))
        reordered = sum(
            1 for before, after in zip(candidates, ordered, strict=True) if before is not after
        )
        return ordered, {
            "band_count": str(len(bands)),
            "max_band_size": str(max(len(band) for band in bands)),
            "reordered": str(reordered),
        }

    def _split_bands(self, candidates: list[ScoredCandidate]) -> list[list[ScoredCandidate]]:
        bands: list[list[ScoredCandidate]] = []
        band_start = candidates[0].score
        current: list[ScoredCandidate] = []
        for candidate in candidates:
            if candidate.score >= band_start - self._score_band - _BAND_EPSILON:
                current.append(candidate)
            else:
                bands.append(current)
                current = [candidate]
                band_start = candidate.score
        bands.append(current)
        return bands

    def _secondary_key(self, candidate: ScoredCandidate, now: datetime) -> float:
        unit = getattr(candidate, "unit", None)
        if unit is None:
            # 未物化候选（无 MemoryUnit）：重要度兜底、时效性不贡献。
            return FALLBACK_IMPORTANCE * self._importance_weight
        importance = parse_importance_value(unit.system_metadata.get("importance"))
        temporal = getattr(unit, "temporal", None)
        t_ingest = temporal.t_ingest if temporal is not None else None
        recency = recency_value(t_ingest, now, self._recency_tau_days)
        return importance * self._importance_weight + recency * self._recency_weight


# -- 注册到 SecondaryScorerProducer（实现自注册） --------------------------------- #


@SecondaryScorerProducer.register("default")
def _build(config):
    return DefaultSecondaryScorer(
        score_band=float(Factory.cfg_get(config, "score_band", DEFAULT_SCORE_BAND)),
        importance_weight=float(
            Factory.cfg_get(config, "importance_weight", DEFAULT_IMPORTANCE_WEIGHT)
        ),
        recency_weight=float(Factory.cfg_get(config, "recency_weight", DEFAULT_RECENCY_WEIGHT)),
        recency_tau_days=float(
            Factory.cfg_get(config, "recency_tau_days", DEFAULT_RECENCY_TAU_DAYS)
        ),
    )
