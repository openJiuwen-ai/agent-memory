"""Runner——把数据集、采集与指标串起来，产出一次评测运行结果。

``Metric`` 是「一批观测 → 一组指标」的可调用：IR 套件、性能套件、QA judge 都按此
签名注入，Runner 不感知具体指标。调用方通过 ``api`` / ``api_factory`` 注入
evaluation baseline adapter 或后续真实 MemoryAPI adapter。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from .harness import ApiFactory, EvalHarness
from .types import CaseOutcome, Dataset, MetricResult, RunResult

# 指标 = 「全体观测 → 一组聚合指标」。无状态、可组合。
Metric = Callable[[List[CaseOutcome]], List[MetricResult]]

_CONFIG_KEYS = (
    "fuser_backend",
    "discloser_backend",
    "rerank_enabled",
    "vector_enabled",
    "graph_enabled",
)


class Runner:
    """编排一次评测：装配 → 采集 → 逐指标聚合。"""

    def __init__(
        self,
        metrics: Sequence[Metric],
        api: Optional[Any] = None,
        api_factory: Optional[ApiFactory] = None,
    ) -> None:
        self._metrics = list(metrics)
        self._api = api
        self._api_factory = api_factory

    def run(self, dataset: Dataset, config: Optional[Any] = None) -> RunResult:
        harness = EvalHarness(api=self._api, api_factory=self._api_factory, config=config)
        outcomes = harness.evaluate(dataset)
        results: List[MetricResult] = []
        for metric in self._metrics:
            results.extend(metric(outcomes))
        return RunResult(
            dataset=getattr(dataset, "name", "dataset"),
            n_queries=len(outcomes),
            metrics=results,
            per_case=outcomes,
            config_summary=_summarize(config),
        )


def _summarize(config: Optional[Any]) -> dict[str, str]:
    if config is None:
        return {}
    return {key: str(getattr(config, key, "")) for key in _CONFIG_KEYS}
