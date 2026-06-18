"""组件级 IR 排序质量指标：Recall@k / Precision@k / MRR / nDCG@k / MAP。

全部确定性、不需 LLM——输入是单 query 的有序候选 id + 相关 id 集，输出 [0,1] 分。
聚合对全体「有 ground truth」的 query 取宏平均（macro average）；无 ground truth 的 query 不计入。

二元相关（relevant ∈ {0,1}），nDCG 用 ``1/log2(rank+1)`` 增益、理想序为全部相关项
排在最前。这是对话记忆类 ground truth（相关/不相关，无分级）最常见的口径。
"""

from __future__ import annotations

import math
from typing import Callable, List, Sequence, Set

from evaluation.core.types import CaseOutcome, MetricResult


def recall_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    if not relevant:
        return 0.0
    hit = sum(1 for uid in ranked[:k] if uid in relevant)
    return hit / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    hit = sum(1 for uid in ranked[:k] if uid in relevant)
    return hit / k


def reciprocal_rank(ranked: Sequence[str], relevant: Set[str]) -> float:
    for idx, uid in enumerate(ranked):
        if uid in relevant:
            return 1.0 / (idx + 1)
    return 0.0


def average_precision(ranked: Sequence[str], relevant: Set[str]) -> float:
    if not relevant:
        return 0.0
    hits = 0
    acc = 0.0
    for idx, uid in enumerate(ranked):
        if uid in relevant:
            hits += 1
            acc += hits / (idx + 1)
    return acc / len(relevant)


def ndcg_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(idx + 2)
        for idx, uid in enumerate(ranked[:k])
        if uid in relevant
    )
    ideal = sum(1.0 / math.log2(idx + 2) for idx in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def _macro(graded: List[CaseOutcome], fn: Callable[[CaseOutcome], float]) -> float:
    if not graded:
        return 0.0
    return sum(fn(outcome) for outcome in graded) / len(graded)


def ir_metrics(ks: Sequence[int] = (1, 3, 5, 10)):
    """构造 IR 指标套件（一个 :data:`evaluation.core.runner.Metric`）。"""

    def _metric(outcomes: List[CaseOutcome]) -> List[MetricResult]:
        graded = [outcome for outcome in outcomes if outcome.relevant_unit_ids]
        detail = {"graded_queries": float(len(graded))}
        results: List[MetricResult] = []
        for k in ks:
            results.append(
                MetricResult(
                    f"recall@{k}",
                    _macro(
                        graded,
                        lambda outcome, k=k: recall_at_k(
                            outcome.ranked_unit_ids,
                            outcome.relevant_unit_ids,
                            k,
                        ),
                    ),
                    dict(detail),
                )
            )
            results.append(
                MetricResult(
                    f"precision@{k}",
                    _macro(
                        graded,
                        lambda outcome, k=k: precision_at_k(
                            outcome.ranked_unit_ids,
                            outcome.relevant_unit_ids,
                            k,
                        ),
                    ),
                    dict(detail),
                )
            )
            results.append(
                MetricResult(
                    f"ndcg@{k}",
                    _macro(
                        graded,
                        lambda outcome, k=k: ndcg_at_k(
                            outcome.ranked_unit_ids,
                            outcome.relevant_unit_ids,
                            k,
                        ),
                    ),
                    dict(detail),
                )
            )
        results.append(
            MetricResult(
                "mrr",
                _macro(
                    graded,
                    lambda outcome: reciprocal_rank(
                        outcome.ranked_unit_ids,
                        outcome.relevant_unit_ids,
                    ),
                ),
                dict(detail),
            )
        )
        results.append(
            MetricResult(
                "map",
                _macro(
                    graded,
                    lambda outcome: average_precision(
                        outcome.ranked_unit_ids,
                        outcome.relevant_unit_ids,
                    ),
                ),
                dict(detail),
            )
        )
        return results

    return _metric
