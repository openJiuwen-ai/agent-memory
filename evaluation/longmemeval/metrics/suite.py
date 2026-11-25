"""把「并发度决定了哪些指标可信」编码成一处。

性能指标测的是 wall-clock 延迟。并发执行时，单次请求的耗时包含排队等待，
测出来的是「当前并发下的排队延迟」而非「系统的检索速度」，因此不可用。
让这个取舍在构造函数里发生，调用方无法拼出非法组合。
"""
from __future__ import annotations

from collections.abc import Callable

from ..types import CaseOutcome, MetricResult
from .ir_metrics import ir_metrics
from .perf_metrics import perf_metrics
from .qa_metrics import qa_accuracy

Metric = Callable[[list[CaseOutcome]], list[MetricResult]]


def build_metric_suite(
    *,
    concurrency: int = 1,
    judge: object | None = None,
    ks: tuple[int, ...] = (10, 40, 50),
) -> list[Metric]:
    """按并发度返回可信的指标套件。

    ``concurrency == 1`` 时包含性能指标；并发执行时剔除，因为 wall-clock
    测量会被排队污染。
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    suite: list[Metric] = [ir_metrics(ks=ks)]
    if concurrency == 1:
        suite.append(perf_metrics())
    if judge is not None:
        suite.append(qa_accuracy(judge=judge, concurrency=concurrency))
    return suite
