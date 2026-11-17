# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 性能基线工具的纯函数（分位数计算）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_shards.py"
_spec = importlib.util.spec_from_file_location("benchmark_shards", _SCRIPT)
assert _spec is not None and _spec.loader is not None
benchmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = benchmark
_spec.loader.exec_module(benchmark)


def test_percentiles_of_known_samples() -> None:
    stats = benchmark.percentiles([float(value) for value in range(1, 101)])
    assert stats["p50"] == pytest.approx(50.5, abs=1)
    assert stats["p95"] == pytest.approx(95.05, abs=1)
    assert stats["p99"] == pytest.approx(99.01, abs=1)
    assert stats["mean"] == pytest.approx(50.5)


def test_percentiles_empty() -> None:
    assert benchmark.percentiles([]) == {
        "p50": 0.0,
        "p95": 0.0,
        "p99": 0.0,
        "mean": 0.0,
    }
