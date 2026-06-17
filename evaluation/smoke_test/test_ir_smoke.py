"""评测框架冒烟：JSONL ground truth 解析 + 指标计算，确定性断言。

显式运行：``pytest evaluation/smoke_test``（不在默认 testpaths 内）。
当前使用 evaluation-only in-memory baseline adapter 跑通 write→recall 的 E2E 回归。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from evaluation.benchmark.jsonl_dataset import JsonlDataset
from evaluation.api_adapter import build_evaluation_api
from evaluation.core.types import CaseOutcome
from evaluation.core.runner import Runner
from evaluation.metrics.ir_metrics import ir_metrics
from evaluation.metrics.perf_metrics import perf_metrics

_GOLDEN = os.path.join(os.path.dirname(__file__), "golden_ir.jsonl")


@pytest.fixture(scope="module")
def dataset():
    return JsonlDataset(_GOLDEN)


def _metric(metrics, name: str) -> float:
    return next(m.value for m in metrics if m.name == name)


def test_dataset_loaded(dataset) -> None:
    assert len(dataset.seeds()) == 6
    assert len(dataset.queries()) == 4


def test_ground_truth_keys_loaded(dataset) -> None:
    queries = {q.query_id: q for q in dataset.queries()}
    assert queries["q1"].relevant_keys == {"m1"}
    assert queries["q1"].top_k == 5


def test_ir_metrics_on_synthetic_outcomes() -> None:
    outcomes = [
        CaseOutcome(
            query_id="q1",
            query_text="coffee",
            ranked_unit_ids=["u1", "u2"],
            relevant_unit_ids={"u1"},
            contents=["Alice prefers iced americano"],
            trajectory=[],
        ),
        CaseOutcome(
            query_id="q2",
            query_text="memory subsystem",
            ranked_unit_ids=["u3", "u2"],
            relevant_unit_ids={"u2"},
            contents=["Agent memory subsystem"],
            trajectory=[],
        ),
    ]
    metrics = ir_metrics(ks=(1, 2))(outcomes)
    assert _metric(metrics, "recall@2") == 1.0
    assert _metric(metrics, "mrr") == 0.75


def test_perf_metrics_on_synthetic_trajectory() -> None:
    outcomes = [
        CaseOutcome(
            query_id="q1",
            query_text="coffee",
            ranked_unit_ids=["u1"],
            relevant_unit_ids={"u1"},
            contents=["Alice prefers iced americano"],
            trajectory=[
                SimpleNamespace(stage="parse", cost_ms=1.0),
                SimpleNamespace(stage="recall", cost_ms=3.0),
            ],
        )
    ]
    metrics = perf_metrics()(outcomes)
    assert _metric(metrics, "latency_ms.p50") == 4.0
    assert _metric(metrics, "retrieved_tokens.avg") > 0.0


def test_end_to_end_with_baseline_api_adapter(dataset) -> None:
    runner = Runner(
        [ir_metrics(ks=(1, 3, 5)), perf_metrics()],
        api_factory=build_evaluation_api,
    )
    result = runner.run(dataset)
    assert result.n_queries == 4
    assert _metric(result.metrics, "recall@5") >= 0.75
    assert _metric(result.metrics, "mrr") > 0.3
    assert _metric(result.metrics, "retrieved_tokens.avg") > 0.0
