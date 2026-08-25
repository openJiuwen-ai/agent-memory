"""性能/效率指标（对接架构「可观测：检索 token、p50/p95 时延、容量」）。

直接吃检索轨迹（``with_trajectory=True`` 时每步带 ``cost_ms``/``candidate_count``），
不另造观测。报告：端到端时延 p50/p95、分阶段平均耗时、返回内容的估算 token 量
（披露后实际回传给上层的 token，反映 token 预算友好度）。
"""

from __future__ import annotations

from collections.abc import Sequence

from evaluation.core.types import CaseOutcome, MetricResult


def _percentile(values: Sequence[float], pct: float) -> float:
    """执行 `percentile` 操作。

    Args:
        values: 参数 values（Sequence[float]）。
        pct: 参数 pct（float）。

    Returns:
        返回 float。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _estimate_tokens(text: str) -> int:
    """执行 `estimate_tokens` 操作。

    Args:
        text: 参数 text（str）。

    Returns:
        返回 int。
    """
    return max(1, (len(text) + 3) // 4) if text else 0


def perf_metrics():
    """构造性能指标套件（一个 :data:`evaluation.core.runner.Metric`）。"""

    def _metric(outcomes: list[CaseOutcome]) -> list[MetricResult]:
        """执行 `metric` 操作。

        Args:
            outcomes: 参数 outcomes（list[CaseOutcome]）。

        Returns:
            返回 list[MetricResult]。
        """
        latencies = [
            sum(getattr(step, "cost_ms", 0.0) for step in outcome.trajectory)
            for outcome in outcomes
            if outcome.trajectory
        ]
        # 分阶段平均耗时（按 stage 聚合）。
        stage_totals: dict[str, float] = {}
        stage_counts: dict[str, int] = {}
        for outcome in outcomes:
            for step in outcome.trajectory:
                stage = getattr(step, "stage", "?")
                stage_totals[stage] = stage_totals.get(stage, 0.0) + getattr(step, "cost_ms", 0.0)
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
        stage_avg = {
            f"{stage}_ms": stage_totals[stage] / stage_counts[stage] for stage in stage_totals
        }

        token_counts = [
            sum(_estimate_tokens(content) for content in outcome.contents) for outcome in outcomes
        ]
        avg_tokens = sum(token_counts) / len(token_counts) if token_counts else 0.0

        return [
            MetricResult("latency_ms.p50", _percentile(latencies, 50), stage_avg),
            MetricResult("latency_ms.p95", _percentile(latencies, 95)),
            MetricResult("retrieved_tokens.avg", avg_tokens),
        ]

    return _metric
