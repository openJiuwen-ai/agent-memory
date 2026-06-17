"""组件级 IR 评测入口——灌库 → 跑 query → 算 IR/性能指标 → 出报告。

    python3 evaluation/scripts/run_ir_eval.py [--dataset PATH.jsonl] [--json OUT.json]

确定性、无需 LLM。缺省读取内置 ground truth（smoke_test/golden_ir.jsonl）。
当前默认使用 evaluation-only in-memory baseline adapter；后续真实 MemoryAPI
adapter 接入后可替换 ``api_factory``。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from types import SimpleNamespace
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DATASET = os.path.join(_ROOT, "evaluation", "smoke_test", "golden_ir.jsonl")
_LOGGER = logging.getLogger("evaluation.run_ir_eval")


def _ensure_import_path() -> None:
    for path in (os.path.join(_ROOT, "src"), _ROOT):
        if path not in sys.path:
            sys.path.append(path)


def _load_symbol(module_name: str, symbol_name: str) -> Any:
    return getattr(importlib.import_module(module_name), symbol_name)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="组件级 IR 评测")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET, help="JSONL ground truth 路径")
    parser.add_argument("--fuser", default=None, help="真实 adapter 接入后覆盖 fuser_backend")
    parser.add_argument("--discloser", default=None, help="真实 adapter 接入后覆盖 discloser_backend")
    parser.add_argument("--json", default=None, help="把机读报告写到此路径")
    args = parser.parse_args()

    config = None
    if args.fuser or args.discloser:
        config = SimpleNamespace(
            fuser_backend=args.fuser or "",
            discloser_backend=args.discloser or "",
        )

    _ensure_import_path()
    jsonl_dataset_cls = _load_symbol(
        "evaluation.benchmark.jsonl_dataset",
        "JsonlDataset",
    )
    runner_cls = _load_symbol("evaluation.core.runner", "Runner")
    build_evaluation_api = _load_symbol("evaluation.api_adapter", "build_evaluation_api")
    to_json = _load_symbol("evaluation.core.report", "to_json")
    to_markdown = _load_symbol("evaluation.core.report", "to_markdown")
    ir_metrics = _load_symbol("evaluation.metrics.ir_metrics", "ir_metrics")
    perf_metrics = _load_symbol("evaluation.metrics.perf_metrics", "perf_metrics")

    dataset = jsonl_dataset_cls(args.dataset)
    runner = runner_cls([ir_metrics(), perf_metrics()], api_factory=build_evaluation_api)
    result = runner.run(dataset, config=config)

    _LOGGER.info(to_markdown(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(to_json(result), fh, ensure_ascii=False, indent=2)
        _LOGGER.info("\n[json] %s", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
