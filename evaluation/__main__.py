"""两套独立 SSH 测评实现的统一配置入口。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from evaluation.shared.env import EVALUATION_DIR, load_evaluation_env

_DEFAULT_CONFIG = EVALUATION_DIR / "config.yml"


def _resolve_path(config_file: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = config_file.parent / path
    return str(path.resolve())


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"启动配置不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise RuntimeError("启动配置根节点必须是映射")
    return config


def _longmemeval_args(config_file: Path, settings: dict[str, Any]) -> list[str]:
    output_root = _resolve_path(
        config_file,
        str(settings.get("output_root", "outputs/longmemeval")),
    )
    output_dir = Path(output_root) / datetime.now().strftime("%Y%m%d-%H%M%S")
    args = [
        "--data",
        _resolve_path(
            config_file,
            str(settings.get("data", "datasets/longmemeval/longmemeval_mini.json")),
        ),
        "--config",
        _resolve_path(config_file, str(settings.get("memory_config", "longmemeval/config.yml"))),
        "--output-dir",
        str(output_dir),
        "--max-questions",
        str(settings.get("max_questions", 1)),
        "--scope-org",
        str(settings.get("scope_org", "longmemeval-0804")),
        "--granularity",
        str(settings.get("granularity", "dialogue_turn")),
        "--dialogue-turn-max-chars",
        str(settings.get("dialogue_turn_max_chars", 4096)),
        "--recall-top-k",
        str(settings.get("recall_top_k", 200)),
        "--answer-cutoff",
        str(settings.get("answer_cutoff", 50)),
        "--answer-cutoffs",
        ",".join(str(value) for value in settings.get("answer_cutoffs", [50, 10, 20])),
        "--concurrency",
        str(settings.get("concurrency", 1)),
    ]
    question_ids = [str(value) for value in settings.get("question_ids", []) if str(value)]
    sample_indices = settings.get("sample_indices", [0])
    if question_ids:
        for question_id in question_ids:
            args.extend(["--question-id", question_id])
    elif sample_indices is not None:
        args.extend(["--samples", ",".join(str(value) for value in sample_indices)])
    args.append(
        "--oracle-sessions"
        if settings.get("oracle_sessions", True)
        else "--no-oracle-sessions"
    )
    args.append("--infer" if settings.get("infer", True) else "--no-infer")
    if settings.get("judge_strict", False):
        args.append("--judge-strict")
    os.environ["LONGMEMEVAL_PROXY_RETRY_ATTEMPTS"] = str(
        settings.get("proxy_retry_attempts", 4)
    )
    return args


def _locomo_args(config_file: Path, settings: dict[str, Any]) -> list[str]:
    conversation_ids = settings.get("conversation_ids", [0])
    return [
        "--data",
        _resolve_path(config_file, str(settings.get("data", "datasets/locomo/locomo10.json"))),
        "--config",
        _resolve_path(config_file, str(settings.get("memory_config", "locomo/config.yml"))),
        "--output-root",
        _resolve_path(config_file, str(settings.get("output_root", "outputs/locomo"))),
        "--conversation-ids",
        ",".join(str(value) for value in conversation_ids),
        "--max-sessions",
        str(settings.get("max_sessions", 0)),
        "--max-turns",
        str(settings.get("max_turns", 5)),
        "--max-qa",
        str(settings.get("max_qa", 2)),
        "--concurrency",
        str(settings.get("concurrency", 1)),
        "--recall-top-k",
        str(settings.get("recall_top_k", 200)),
        "--cutoffs",
        ",".join(str(value) for value in settings.get("cutoffs", [10, 20, 50, 200])),
        "--run-tag",
        str(settings.get("run_tag", "local-smoke")),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-memory 独立端到端测评")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="统一启动配置 YAML")
    parser.add_argument(
        "--benchmark",
        choices=("longmemeval", "locomo"),
        default=None,
        help="临时覆盖配置中的 benchmark",
    )
    args = parser.parse_args(argv)

    load_evaluation_env()
    if os.getenv("MILVUS_URI") and not os.getenv("MEM2_MILVUS_URI"):
        os.environ["MEM2_MILVUS_URI"] = os.environ["MILVUS_URI"]
    config_file = Path(args.config).resolve()
    config = _load_config(config_file)
    benchmark = args.benchmark or str(config.get("benchmark", "longmemeval"))
    if benchmark not in ("longmemeval", "locomo"):
        raise RuntimeError("benchmark 只能是 longmemeval 或 locomo")
    settings = config.get(benchmark, {})
    if not isinstance(settings, dict):
        raise RuntimeError(f"{benchmark} 配置必须是映射")

    if benchmark == "longmemeval":
        from evaluation.longmemeval.entry import main as benchmark_main

        return benchmark_main(_longmemeval_args(config_file, settings))

    from evaluation.locomo.entry import main as benchmark_main

    return benchmark_main(_locomo_args(config_file, settings))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[evaluation] {exc}", file=sys.stderr)
        raise SystemExit(2) from None
