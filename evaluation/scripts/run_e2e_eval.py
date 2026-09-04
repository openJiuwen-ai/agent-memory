# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""端到端 benchmark 评测入口（骨架）——write→recall→(LLM judge) 答案级评测。

    python3 evaluation/scripts/run_e2e_eval.py --dataset locomo [--data PATH]

与 IR 评测共用 harness/runner，额外挂 ``qa_accuracy``。要真正出 QA 分，需：
  1) 补全数据集适配器（如 :class:`evaluation.benchmark.locomo_adapter.LoCoMoDataset`）；
  2) 注入 LLM judge（``qa_accuracy(judge=...)``）。

未注入 judge 时仍会跑 IR/性能指标，QA 行标记 skipped——即可先观察检索召回是否覆盖
证据，再决定接 judge。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from importlib import import_module

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

to_markdown = import_module("evaluation.core.report").to_markdown
Runner = import_module("evaluation.core.runner").Runner
ir_metrics = import_module("evaluation.metrics.ir_metrics").ir_metrics
_llm_judge_module = import_module("evaluation.metrics.llm_judge")
LLMJudge = _llm_judge_module.LLMJudge
openai_chat = _llm_judge_module.openai_chat
perf_metrics = import_module("evaluation.metrics.perf_metrics").perf_metrics
qa_accuracy = import_module("evaluation.metrics.qa_metrics").qa_accuracy
logger = logging.getLogger(__name__)


def _load_dataset(name: str, data: str | None):
    if name == "locomo":
        from evaluation.benchmark.locomo_adapter import LoCoMoDataset

        return LoCoMoDataset(data) if data else LoCoMoDataset()
    if name == "longmemeval":
        from evaluation.benchmark.longmemeval_adapter import LongMemEvalDataset

        return LongMemEvalDataset(data) if data else LongMemEvalDataset()
    if name.endswith(".jsonl"):
        from evaluation.benchmark.jsonl_dataset import JsonlDataset

        return JsonlDataset(name)
    raise ValueError(f"未知数据集：{name}（支持 'locomo' / 'longmemeval' 或 *.jsonl 路径）")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="端到端 QA 评测")
    parser.add_argument(
        "--dataset",
        default="locomo",
        help="'locomo' / 'longmemeval' 或 *.jsonl 路径",
    )
    parser.add_argument("--data", default=None, help="数据集原始文件路径（如 locomo10.json）")
    # judge（OpenAI 兼容；缺省读环境变量）；三者齐备才启用，否则 QA 跳过。
    parser.add_argument("--judge-model", default=os.getenv("JUDGE_MODEL"), help="judge 模型名")
    parser.add_argument(
        "--judge-base-url",
        default=os.getenv("JUDGE_BASE_URL"),
        help="judge OpenAI 兼容 base_url",
    )
    parser.add_argument("--judge-api-key", default=os.getenv("JUDGE_API_KEY"), help="judge API key")
    parser.add_argument("--judge-strict", action="store_true", help="用严格判分 prompt")
    args = parser.parse_args()

    try:
        dataset = _load_dataset(args.dataset, args.data)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    judge = None
    if args.judge_model and args.judge_base_url and args.judge_api_key:
        chat = openai_chat(args.judge_base_url, args.judge_model, args.judge_api_key)
        judge = LLMJudge(chat=chat, strict=args.judge_strict)

    runner = Runner([ir_metrics(), perf_metrics(), qa_accuracy(judge=judge)])
    result = runner.run(dataset)
    logger.info(to_markdown(result))
    if judge is None:
        logger.info(
            "\n[note] 未配置 judge（需 --judge-model/--judge-base-url/--judge-api-key "
            "或对应环境变量），"
            "qa_accuracy 已跳过；仅出 IR/性能。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
