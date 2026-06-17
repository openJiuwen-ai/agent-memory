"""LongMemEval 适配器解析 + 端到端冒烟（合成小样本，确定性、无需 LLM/真实数据集）。

显式运行：``pytest evaluation/smoke_test``。验证：
1. turn→seed / question→query / has_answer&answer_session_ids→relevant_keys 映射；
2. 每个 sample 独立 scope（user=question_id）、question_type 落到 bucket、拒答标记；
3. 端到端 write→recall 跑通，注入桩 judge 能出 qa_accuracy 与按 question_type 分桶。
"""

from __future__ import annotations

import os

import pytest

from evaluation.benchmark.longmemeval_adapter import LongMemEvalDataset
from evaluation.api_adapter import build_evaluation_api
from evaluation.core.runner import Runner
from evaluation.metrics.ir_metrics import ir_metrics
from evaluation.metrics.qa_metrics import qa_accuracy

_MINI = os.path.join(os.path.dirname(__file__), "longmemeval_mini.json")


@pytest.fixture(scope="module")
def dataset() -> LongMemEvalDataset:
    return LongMemEvalDataset(_MINI)


def test_seeds_per_turn_isolated_by_question(dataset) -> None:
    seeds = {s.key: s for s in dataset.seeds()}
    # lme_0: s1(2 turns) + s2(2 turns) = 4；lme_1_abs: s1(2 turns) = 2
    assert len(seeds) == 6
    assert seeds["lme_0/s1#0"].content.startswith("[user]: I just adopted a golden retriever")
    assert seeds["lme_0/s1#0"].scope.user == "lme_0"
    assert seeds["lme_1_abs/s1#0"].scope.user == "lme_1_abs"  # 每题独立 scope


def test_queries_evidence_bucket_and_abstention(dataset) -> None:
    queries = {q.query_id: q for q in dataset.queries()}
    # turn 级 has_answer 优先：lme_0 的证据是 s1#0
    assert queries["lme_0"].relevant_keys == {"lme_0/s1#0"}
    assert queries["lme_0"].metadata["bucket"] == "single-session-user"
    assert queries["lme_0"].metadata["question_date"] == "2023/05/20 (Sat) 10:30"
    # 拒答题：question_id 以 _abs 结尾，无证据
    assert queries["lme_1_abs"].metadata["abstention"] == "true"
    assert queries["lme_1_abs"].relevant_keys == set()


def test_end_to_end_with_stub_judge(dataset) -> None:
    # 桩 judge：拒答题看是否「不知道」，否则看 ground truth 关键词是否被召回——确定性。
    def stub_judge(query: str, expected: str, contexts, meta=None) -> float:
        if (meta or {}).get("abstention") == "true":
            return 1.0  # 桩：视作正确拒答
        return 1.0 if any("retriever" in c.lower() for c in contexts) else 0.0

    runner = Runner(
        [ir_metrics(ks=(5,)), qa_accuracy(judge=stub_judge)],
        api_factory=build_evaluation_api,
    )
    result = runner.run(dataset)

    qa = next(m for m in result.metrics if m.name == "qa_accuracy")
    assert qa.detail["qa_cases"] == 2.0
    # 按 question_type 分桶
    assert any(k.startswith("acc:") for k in qa.detail)
    # 证据召回质量（lme_0 的 s1#0 应被召回）
    recall5 = next(m.value for m in result.metrics if m.name == "recall@5")
    assert recall5 > 0.0
