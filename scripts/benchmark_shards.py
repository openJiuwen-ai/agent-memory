#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09 性能基线采集工具：对**当前配置**跑固定负载，输出 P50/P95/P99。

设计要求的基线是「同一硬件 / 数据集 / 模型 / 参数下，未分片 vs shared vs split
分别跑」。本工具一次只测当前配置；分别用三份配置运行后对比结果即可。它不生成
或伪造结论数字——实际表格必须在目标部署上运行后填写进 F09「性能基线」。

用法::

    scripts/benchmark_shards.py --config prod.yaml --iterations 1000 --top-k 10
    scripts/benchmark_shards.py --config prod.yaml --json report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from jiuwen_memory.common.log import get_logger  # noqa: E402
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, ParsedQuery  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.storage.bootstrap import register_backends  # noqa: E402
from jiuwen_memory.storage.store_manager import StoreManagerProducer  # noqa: E402

logger = get_logger(__name__)


class BenchmarkError(Exception):
    """输入 / 数据不满足基准条件：由 ``main`` 转成退出码。"""


def percentiles(samples: list[float]) -> dict[str, float]:
    """毫秒分位数（P50/P95/P99）；空样本返回 0。"""
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}
    ordered = sorted(samples)

    def pick(ratio: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
        return ordered[index]

    return {
        "p50": pick(0.50),
        "p95": pick(0.95),
        "p99": pick(0.99),
        "mean": statistics.fmean(ordered),
    }


def _timed(operation) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        operation()
        return (time.perf_counter() - started) * 1000.0, True
    except Exception:  # noqa: BLE001 - 错误率单独统计，不中断基准
        return (time.perf_counter() - started) * 1000.0, False


def run(config_path: str, iterations: int, top_k: int) -> dict:
    register_backends()
    ctx = Config.from_yaml(config_path).context()
    manager = StoreManagerProducer.build_named(
        str(ctx.globals.get("store_manager", "default")), ctx
    )
    domain = manager.domain_store("default")
    kv = manager.kv()
    scopes = [scope for scope in kv.scopes() if kv.scan(scope, MEMORY_KEY_PREFIX)]
    if not scopes:
        raise BenchmarkError("no memory data found for the configured store manager")
    scope = scopes[0]
    keys = [key for key, _ in kv.scan(scope, MEMORY_KEY_PREFIX)]
    report: dict = {"config": config_path, "scopes": len(scopes), "units": len(keys)}

    def measure(name: str, operation) -> None:
        samples: list[float] = []
        errors = 0
        for index in range(iterations):
            elapsed, ok = _timed(lambda: operation(index))
            samples.append(elapsed)
            errors += 0 if ok else 1
        stats = percentiles(samples)
        stats["errors"] = errors
        stats["iterations"] = iterations
        report[name] = stats

    measure("kv_get", lambda index: kv.get(scope, keys[index % len(keys)]))
    measure("kv_list", lambda index: kv.list(scope, offset=0, limit=min(100, len(keys))))
    measure("kv_scan", lambda index: kv.scan(scope, MEMORY_KEY_PREFIX))
    shards = list(getattr(kv, "shards", []))
    extensions = {"shards": shards} if shards else {}

    def recall_once(index: int) -> None:
        domain.recall(
            scope,
            ParsedQuery(raw="benchmark", extensions=dict(extensions)),
            channels=None,
            recall_limit=top_k,
        )

    measure("recall", recall_once)
    return report


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", default="", help="把结果写入 JSON 文件")
    args = parser.parse_args(argv)
    if args.iterations <= 0:
        raise BenchmarkError("--iterations must be positive")
    report = run(args.config, args.iterations, args.top_k)
    for name in ("kv_get", "kv_list", "kv_scan", "recall"):
        stats = report[name]
        logger.info(
            f"{name:10s} p50={stats['p50']:.2f}ms p95={stats['p95']:.2f}ms "
            f"p99={stats['p99']:.2f}ms mean={stats['mean']:.2f}ms "
            f"errors={stats['errors']}/{stats['iterations']}"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info(f"report written to {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：把输入 / 环境错误转成退出码，只有进程入口使用 ``SystemExit``。"""
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True
    )
    try:
        return _run(argv)
    except BenchmarkError as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
