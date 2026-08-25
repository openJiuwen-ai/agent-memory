"""Runner——把数据集、采集与指标串起来，产出一次评测运行结果。

``Metric`` 是「一批观测 → 一组指标」的可调用：IR 套件、性能套件、QA judge 都按此
签名注入，Runner 不感知具体指标。对比不同 Config（如 ``rrf`` vs ``weighted_rrf``、
``truncating`` vs ``structured``）只需用不同 config 各跑一次。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from jiuwen_memory.config.config import Config
from jiuwen_memory.config.defaults import default_context

from .harness import EvalHarness
from .types import CaseOutcome, Dataset, MetricResult, RunResult

# 指标 = 「全体观测 → 一组聚合指标」。无状态、可组合。
Metric = Callable[[List[CaseOutcome]], List[MetricResult]]

# 报告摘要字段 → 新两级配置的取值口径：
#   *_backend：对应 producer 顶层命名空间下 default 实例的 target；
#   *_enabled：globals 跨切面开关。
_BACKEND_KEYS = {"fuser_backend": "fuser", "discloser_backend": "discloser"}
_GLOBAL_KEYS = ("rerank_enabled", "vector_enabled", "graph_enabled")


class Runner:
    """编排一次评测：装配 → 采集 → 逐指标聚合。"""

    def __init__(self, metrics: Sequence[Metric]) -> None:
        """初始化 Runner。

        Args:
            metrics: 参数 metrics（Sequence[Metric]）。
        """
        self._metrics = list(metrics)

    def run(self, dataset: Dataset, config: Optional[Config] = None) -> RunResult:
        """执行当前任务并返回结果。

        Args:
            dataset: 参数 dataset（Dataset）。
            config: 参数 config（Optional[Config]）。

        Returns:
            返回 RunResult。
        """
        harness = EvalHarness(config=config)
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


def _summarize(config: Optional[Config]) -> dict[str, str]:
    """报告用的配置摘要：与 build_kernel 同口径，把用户配置合并覆盖到内置默认之上读「生效值」。

    故无 ``--fuser`` 时也会如实显示生效的默认（fuser_backend=rrf / discloser_backend=truncating），
    给了覆盖则显示覆盖后的实现名——供后续对比实验与 CI 留档准确比对。
    """
    cfg = config or Config()
    ctx = default_context()
    if not cfg.is_empty():
        ctx = ctx.merged(cfg.context())  # 不传 known_top_names：仅摘要、不做顶层名校验
    summary: dict[str, str] = {}
    for report_key, top in _BACKEND_KEYS.items():
        inst = ctx.namespaces.get(top, {}).get("default")
        summary[report_key] = inst.target if inst is not None else ""
    for key in _GLOBAL_KEYS:
        summary[key] = str(ctx.globals.get(key, ""))
    return summary
