"""评测框架冒烟：用小型评测标注集跑通 装配→灌库→召回→IR/性能指标，确定性断言。

显式运行：``pytest evaluation/smoke_test``（不在默认 testpaths 内）。
既验证评测框架自身可用，也作为检索召回质量的最低回归基线。
"""

from __future__ import annotations

import os

import pytest

from evaluation.benchmark.jsonl_dataset import JsonlDataset
from evaluation.core.runner import Runner
from evaluation.metrics.ir_metrics import ir_metrics
from evaluation.metrics.perf_metrics import perf_metrics

pytestmark = pytest.mark.integration

_GOLDEN = os.path.join(os.path.dirname(__file__), "golden_ir.jsonl")


@pytest.fixture(scope="module")
def run_result():
    dataset = JsonlDataset(_GOLDEN)
    runner = Runner([ir_metrics(ks=(1, 3, 5)), perf_metrics()])
    return runner.run(dataset)


def _metric(result, name: str) -> float:
    return next(m.value for m in result.metrics if m.name == name)


def test_dataset_loaded(run_result) -> None:
    assert run_result.n_queries == 4


def test_recall_covers_relevant(run_result) -> None:
    # 每个 query 的相关语料应被召回进 top-5（确定性相关性标注）。
    assert _metric(run_result, "recall@5") >= 0.75


def test_ranking_is_meaningful(run_result) -> None:
    # MRR/nDCG 体现相关项排得靠前，而非仅命中。
    assert _metric(run_result, "mrr") > 0.3
    assert _metric(run_result, "ndcg@5") > 0.3


def test_perf_metrics_present(run_result) -> None:
    assert _metric(run_result, "latency_ms.p50") >= 0.0
    assert _metric(run_result, "retrieved_tokens.avg") > 0.0
