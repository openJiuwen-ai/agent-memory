"""端到端 benchmark 评测入口（骨架）——write→recall→(LLM judge) 答案级评测。

    python3 evaluation/scripts/run_e2e_eval.py --dataset locomo [--data PATH]

与 IR 评测共用 harness/runner，额外挂 ``qa_accuracy``。要真正出 QA 分，需：
  1) 下载公开数据集；
  2) 注入 LLM judge（``qa_accuracy(judge=...)``）。

当前默认使用 evaluation-only in-memory baseline adapter；后续真实 MemoryAPI
adapter 接入后可替换 ``api_factory``。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from typing import Any, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOGGER = logging.getLogger("evaluation.run_e2e_eval")


def _ensure_import_path() -> None:
    for path in (os.path.join(_ROOT, "src"), _ROOT):
        if path not in sys.path:
            sys.path.append(path)


def _load_symbol(module_name: str, symbol_name: str) -> Any:
    return getattr(importlib.import_module(module_name), symbol_name)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def _load_dataset(name: str, data: Optional[str]):
    if name == "locomo":
        dataset_cls = _load_symbol("evaluation.benchmark.locomo_adapter", "LoCoMoDataset")
        return dataset_cls(data) if data else dataset_cls()
    if name == "longmemeval":
        dataset_cls = _load_symbol(
            "evaluation.benchmark.longmemeval_adapter",
            "LongMemEvalDataset",
        )
        return dataset_cls(data) if data else dataset_cls()
    if name.endswith(".jsonl"):
        dataset_cls = _load_symbol(
            "evaluation.benchmark.jsonl_dataset",
            "JsonlDataset",
        )
        return dataset_cls(name)
    raise ValueError(f"未知数据集：{name}（支持 'locomo' / 'longmemeval' 或 *.jsonl 路径）")


def main() -> int:
    _configure_logging()
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

    _ensure_import_path()
    build_evaluation_api = _load_symbol("evaluation.api_adapter", "build_evaluation_api")
    to_markdown = _load_symbol("evaluation.core.report", "to_markdown")
    runner_cls = _load_symbol("evaluation.core.runner", "Runner")
    ir_metrics = _load_symbol("evaluation.metrics.ir_metrics", "ir_metrics")
    llm_judge_cls = _load_symbol("evaluation.metrics.llm_judge", "LLMJudge")
    openai_chat = _load_symbol("evaluation.metrics.llm_judge", "openai_chat")
    perf_metrics = _load_symbol("evaluation.metrics.perf_metrics", "perf_metrics")
    qa_accuracy = _load_symbol("evaluation.metrics.qa_metrics", "qa_accuracy")

    try:
        dataset = _load_dataset(args.dataset, args.data)
    except (FileNotFoundError, ValueError) as exc:
        _LOGGER.error("[dataset required] %s", exc)
        return 2

    judge = None
    if args.judge_model and args.judge_base_url and args.judge_api_key:
        chat = openai_chat(args.judge_base_url, args.judge_model, args.judge_api_key)
        judge = llm_judge_cls(chat=chat, strict=args.judge_strict)

    runner = runner_cls(
        [ir_metrics(), perf_metrics(), qa_accuracy(judge=judge)],
        api_factory=build_evaluation_api,
    )
    try:
        result = runner.run(dataset)
    except FileNotFoundError as exc:
        _LOGGER.error("[dataset required] %s", exc)
        return 2
    except RuntimeError as exc:
        _LOGGER.error("[adapter required] %s", exc)
        _LOGGER.error("wire a MemoryAPI adapter to run end-to-end QA metrics.")
        return 2

    _LOGGER.info(to_markdown(result))
    if judge is None:
        _LOGGER.info(
            "\n[note] 未配置 judge（需 --judge-model/--judge-base-url/--judge-api-key 或对应环境变量），"
            "qa_accuracy 已跳过；仅出 IR/性能。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
