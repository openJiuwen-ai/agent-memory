"""LongMemEval 端到端 QA 答案级指标——需注入 judge。

链路：检索结果（``CaseOutcome.contents``）作为证据 → 由 ``judge`` 给出 [0,1] 正确分
（典型实现：用 LLM 从证据合成答案，再 LLM-as-judge 与 ``expected_answer`` 比对）。

**骨架**：judge 是可插拔回调，未注入时本指标跳过（``skipped=1``），不阻塞 IR 层评测。
公开基准接入时，把对应数据集的 judge prompt 实现成一个 :data:`JudgeFn` 注入即可。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

from evaluation.longmemeval.types import CaseOutcome, MetricResult

# (query_text, expected_answer, retrieved_contents, meta) -> 正确分 [0,1]
# meta 透传 QueryCase.metadata（如 question_date 供时序题、question_type、abstention）。
JudgeFn = Callable[[str, str, Sequence[str], Mapping[str, object]], float]


def qa_accuracy(judge: JudgeFn | None = None, concurrency: int = 1):
    """构造端到端 QA 准确率指标（一个 :data:`evaluation.longmemeval.runner.Metric`）。

    仅对带 ``expected_answer`` 的 case 计分；judge 缺省（None）则标记 skipped。
    """

    def _metric(outcomes: list[CaseOutcome]) -> list[MetricResult]:
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

        def _score(outcome: CaseOutcome):
            metadata = {
                **outcome.metadata,
                "context_dates": outcome.context_dates,
                "context_message_dates": outcome.context_message_dates,
                "context_event_dates": outcome.context_event_dates,
            }
            score_cutoffs = getattr(judge, "score_cutoffs", None)
            if callable(score_cutoffs):
                scores = score_cutoffs(
                    outcome.query_text,
                    outcome.expected_answer,
                    outcome.contents,
                    metadata,
                )
            else:
                primary = int(outcome.metadata.get("answer_cutoff", len(outcome.contents)))
                scores = {
                    primary: judge(
                        outcome.query_text,
                        outcome.expected_answer,
                        outcome.contents,
                        metadata,
                    )
                }
            return outcome, scores

        if concurrency <= 0:
            raise ValueError(f"QA concurrency must be positive, got {concurrency}")
        if concurrency == 1:
            scored = [_score(outcome) for outcome in graded]
        else:
            with ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="qa-eval",
            ) as executor:
                scored = list(executor.map(_score, graded))

        primary_scored = [
            (
                outcome,
                scores[int(outcome.metadata.get("answer_cutoff", len(outcome.contents)))],
            )
            for outcome, scores in scored
        ]
        detail = {"qa_cases": float(len(primary_scored))}
        detail.update(_by_bucket(primary_scored))
        results = [
            MetricResult(
                "qa_accuracy",
                sum(score for _, score in primary_scored) / len(primary_scored),
                detail,
            )
        ]
        all_cutoffs = sorted({cutoff for _, scores in scored for cutoff in scores})
        for cutoff in all_cutoffs:
            cutoff_scored = [
                (outcome, scores[cutoff])
                for outcome, scores in scored
                if cutoff in scores
            ]
            cutoff_detail = {"qa_cases": float(len(cutoff_scored))}
            cutoff_detail.update(_by_bucket(cutoff_scored))
            results.append(
                MetricResult(
                    f"qa_accuracy@{cutoff}",
                    sum(score for _, score in cutoff_scored) / len(cutoff_scored),
                    cutoff_detail,
                )
            )
        return results

    return _metric


def _by_bucket(scored) -> dict:
    """按 ``metadata['bucket']`` 分桶计算各 question_type 的准确率。"""
    totals: dict[str, list] = {}
    for outcome, score in scored:
        label = outcome.metadata.get("bucket", "")
        if not label:
            continue
        acc = totals.setdefault(label, [0.0, 0])
        acc[0] += score
        acc[1] += 1
    return {f"acc:{label}": s / n for label, (s, n) in totals.items() if n}
