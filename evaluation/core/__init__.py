# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""评估框架核心抽象：数据集、采集、编排、报告。"""

from __future__ import annotations

from .harness import EvalHarness
from .report import to_json, to_markdown
from .runner import Metric, Runner
from .types import (
    CaseOutcome,
    Dataset,
    MemorySeed,
    MetricResult,
    QueryCase,
    RunResult,
)

__all__ = [
    "EvalHarness",
    "Runner",
    "Metric",
    "Dataset",
    "MemorySeed",
    "QueryCase",
    "CaseOutcome",
    "MetricResult",
    "RunResult",
    "to_json",
    "to_markdown",
]
