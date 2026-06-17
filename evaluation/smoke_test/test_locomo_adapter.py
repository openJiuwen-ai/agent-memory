"""LoCoMo 适配器解析 + judge 接线冒烟（合成小样本，确定性、无需 LLM/真实数据集）。

显式运行：``pytest evaluation/smoke_test``。验证三件事：
1. turn→seed / qa→query / evidence→relevant_keys 的映射正确；
2. 每个 sample 独立 scope、category 5 被跳过；
3. 端到端跑通（write→recall）后，注入桩 judge 能算出 qa_accuracy 与按类目分桶。
"""

from __future__ import annotations

import os

import pytest

from evaluation.benchmark.locomo_adapter import LoCoMoDataset
from evaluation.api_adapter import build_evaluation_api
from evaluation.core.runner import Runner
from evaluation.metrics.ir_metrics import ir_metrics
from evaluation.metrics.qa_metrics import qa_accuracy

_MINI = os.path.join(os.path.dirname(__file__), "locomo_mini.json")


@pytest.fixture(scope="module")
def dataset() -> LoCoMoDataset:
    return LoCoMoDataset(_MINI)


def test_seeds_cover_all_turns(dataset) -> None:
    seeds = {s.key: s for s in dataset.seeds()}
    # sample_0: session_1(3) + session_2(2) = 5；sample_1: session_1(3) = 3
    assert len(seeds) == 8
    assert seeds["sample_0/D1:1"].content == (
        "[Alice]: I adopted a golden retriever puppy named Max last weekend."
    )
    # 每个 sample 独立 scope（user=sample_id）
    assert seeds["sample_0/D1:1"].scope.user == "sample_0"
    assert seeds["sample_1/D1:1"].scope.user == "sample_1"


def test_queries_map_evidence_and_skip_adversarial(dataset) -> None:
    queries = {q.query_id: q for q in dataset.queries()}
    # category 5 的对抗题应被跳过：sample_0 只剩 2 道
    assert "sample_0_qa2" not in queries
    assert queries["sample_0_qa0"].relevant_keys == {"sample_0/D1:1"}
    assert queries["sample_1_qa0"].relevant_keys == {"sample_1/D1:1", "sample_1/D1:3"}
    assert queries["sample_0_qa1"].metadata["category"] == "4"


def test_end_to_end_with_stub_judge(dataset) -> None:
    # 桩 judge：ground truth 答案的首词若出现在任一召回内容里则判对——确定性，替代真实 LLM。
    def stub_judge(query: str, expected: str, contexts, meta=None) -> float:
        needle = expected.split()[0].lower() if expected else ""
        return 1.0 if any(needle in c.lower() for c in contexts) else 0.0

    runner = Runner(
        [ir_metrics(ks=(5,)), qa_accuracy(judge=stub_judge)],
        api_factory=build_evaluation_api,
    )
    result = runner.run(dataset)

    qa = next(m for m in result.metrics if m.name == "qa_accuracy")
    assert qa.detail["qa_cases"] == 3.0  # 跳过 1 道对抗题后剩 3 道
    assert 0.0 <= qa.value <= 1.0
    # 按类目分桶存在（category 1 与 4）
    assert any(k.startswith("acc:") for k in qa.detail)
