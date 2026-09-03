"""性能指标：公开检索耗时、内部召回诊断、轨迹统计和返回内容 token 估算。"""

from __future__ import annotations

from collections.abc import Sequence

from evaluation.longmemeval.types import CaseOutcome, MetricResult


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def perf_metrics():
    """构造性能指标套件（一个 :data:`evaluation.longmemeval.runner.Metric`）。"""

    def _metric(outcomes: list[CaseOutcome]) -> list[MetricResult]:
        trajectory_stage_sums = [
            sum(
                getattr(step, "cost_ms", 0.0)
                for step in outcome.trajectory
                if getattr(step, "stage", "?") != "recall_wall"
            )
            for outcome in outcomes
            if outcome.trajectory
        ]
        retrieval_e2e_values = [
            float(outcome.memory_retrieval_e2e_wall_ms)
            for outcome in outcomes
            if outcome.memory_retrieval_e2e_wall_ms > 0.0
        ]
        storage_recall_values = [
            float(outcome.storage_recall_wall_ms)
            for outcome in outcomes
            if outcome.storage_recall_wall_ms > 0.0
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

        results = [
            MetricResult(
                "trajectory_stage_sum_ms.p50",
                _percentile(trajectory_stage_sums, 50),
                stage_avg,
            ),
            MetricResult(
                "trajectory_stage_sum_ms.p95",
                _percentile(trajectory_stage_sums, 95),
            ),
            MetricResult("retrieved_tokens.avg", avg_tokens),
        ]
        if retrieval_e2e_values:
            results.append(
                MetricResult(
                    "memory_retrieval_e2e_wall_ms.avg",
                    sum(retrieval_e2e_values) / len(retrieval_e2e_values),
                    {
                        "count": float(len(retrieval_e2e_values)),
                        "p50_ms": _percentile(retrieval_e2e_values, 50),
                        "p95_ms": _percentile(retrieval_e2e_values, 95),
                        "p99_ms": _percentile(retrieval_e2e_values, 99),
                    },
                )
            )
        if storage_recall_values:
            results.append(
                MetricResult(
                    "storage_recall_wall_ms.avg",
                    sum(storage_recall_values) / len(storage_recall_values),
                    {
                        "count": float(len(storage_recall_values)),
                        "p50_ms": _percentile(storage_recall_values, 50),
                        "p95_ms": _percentile(storage_recall_values, 95),
                        "p99_ms": _percentile(storage_recall_values, 99),
                    },
                )
            )
        return results

    return _metric
