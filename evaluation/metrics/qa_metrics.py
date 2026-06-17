"""端到端 QA 答案级指标（对标 LoCoMo / LongMemEval）——需注入 judge。

链路：检索结果（``CaseOutcome.contents``）作为证据 → 由 ``judge`` 给出 [0,1] 正确分
（典型实现：用 LLM 从证据合成答案，再 LLM-as-judge 与 ``expected_answer`` 比对）。

**骨架**：judge 是可插拔回调，未注入时本指标跳过（``skipped=1``），不阻塞 IR 层评测。
公开基准接入时，把对应数据集的 judge prompt 实现成一个 :data:`JudgeFn` 注入即可。
"""

from __future__ import annotations

from typing import Callable, List, Mapping, Optional, Sequence

from evaluation.core.types import CaseOutcome, MetricResult

# (query_text, expected_answer, retrieved_contents, meta) -> 正确分 [0,1]
# meta 透传 QueryCase.metadata（如 question_date 供时序题、question_type、abstention）。
JudgeFn = Callable[[str, str, Sequence[str], Mapping[str, str]], float]


def qa_accuracy(judge: Optional[JudgeFn] = None):
    """构造端到端 QA 准确率指标（一个 :data:`evaluation.core.runner.Metric`）。

    仅对带 ``expected_answer`` 的 case 计分；judge 缺省（None）则标记 skipped。
    """

    def _metric(outcomes: List[CaseOutcome]) -> List[MetricResult]:
        graded = [outcome for outcome in outcomes if outcome.expected_answer]
        if judge is None:
            return [
                MetricResult(
                    "qa_accuracy",
                    0.0,
                    {"skipped": 1.0, "reason": 0.0, "qa_cases": float(len(graded))},
                )
            ]
        if not graded:
            return [MetricResult("qa_accuracy", 0.0, {"qa_cases": 0.0})]
        scored = [
            (
                outcome,
                judge(
                    outcome.query_text,
                    outcome.expected_answer,
                    outcome.contents,
                    outcome.metadata,
                ),
            )
            for outcome in graded
        ]
        detail = {"qa_cases": float(len(graded))}
        detail.update(_by_bucket(scored))
        return [
            MetricResult(
                "qa_accuracy",
                sum(score for _, score in scored) / len(scored),
                detail,
            )
        ]

    return _metric


def _by_bucket(scored) -> dict:
    """按 ``metadata['bucket']`` 分桶出各类准确率（LoCoMo 的 category / LongMemEval 的
    question_type 都落在统一的 ``bucket`` 维度上）。"""
    totals: dict[str, list] = {}
    for outcome, score in scored:
        label = outcome.metadata.get("bucket", "")
        if not label:
            continue
        acc = totals.setdefault(label, [0.0, 0])
        acc[0] += score
        acc[1] += 1
    return {f"acc:{label}": s / n for label, (s, n) in totals.items() if n}
