# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""组件级 IR 评测入口——装配 → 灌库 → 跑 query → 算 IR/性能指标 → 出报告。

    python3 evaluation/scripts/run_ir_eval.py [--dataset PATH.jsonl] [--json OUT.json]

确定性、无需 LLM、无外部依赖。缺省跑内置冒烟评测基准（smoke_test/golden_ir.jsonl）。
可选 ``--fuser`` / ``--discloser`` 切换装配后端，用同一评测标注集对比改动收益。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from importlib import import_module

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

Config = import_module("jiuwen_memory.config.config").Config
JsonlDataset = import_module("evaluation.benchmark.jsonl_dataset").JsonlDataset
_report_module = import_module("evaluation.core.report")
to_json = _report_module.to_json
to_markdown = _report_module.to_markdown
Runner = import_module("evaluation.core.runner").Runner
ir_metrics = import_module("evaluation.metrics.ir_metrics").ir_metrics
perf_metrics = import_module("evaluation.metrics.perf_metrics").perf_metrics

_DEFAULT_DATASET = os.path.join(_ROOT, "evaluation", "smoke_test", "golden_ir.jsonl")
logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="组件级 IR 评测")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET, help="JSONL 评测标注路径")
    parser.add_argument("--fuser", default=None, help="覆盖 fuser_backend（rrf | weighted_rrf）")
    parser.add_argument(
        "--discloser",
        default=None,
        help="覆盖 discloser_backend（truncating | structured）",
    )
    parser.add_argument("--json", default=None, help="把机读报告写到此路径")
    args = parser.parse_args()

    # 两级命名空间配置：只覆盖要切换的 producer 顶层（fuser / discloser）下的 default 实例，
    # 其余沿用内置默认（build_kernel 把本配置合并覆盖到 default_context 之上）。
    overrides: dict = {}
    if args.fuser:
        overrides["fuser"] = {"default": args.fuser}
    if args.discloser:
        overrides["discloser"] = {"default": args.discloser}
    config = Config.from_dict(overrides)

    dataset = JsonlDataset(args.dataset)
    runner = Runner([ir_metrics(), perf_metrics()])
    result = runner.run(dataset, config=config)

    logger.info(to_markdown(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(to_json(result), fh, ensure_ascii=False, indent=2)
        logger.info("\n[json] %s", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
