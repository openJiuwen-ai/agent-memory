"""LongMemEval SSH 测评实现的配置入口。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from evaluation.shared.env import EVALUATION_DIR, load_evaluation_env

_DEFAULT_CONFIG = EVALUATION_DIR / "config.yml"
logger = logging.getLogger(__name__)


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
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(output_root) / timestamp
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-memory LongMemEval 端到端测评")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="启动配置 YAML")
    parser.add_argument(
        "--benchmark",
        choices=("longmemeval",),
        default=None,
        help="兼容统一入口参数；当前分支只提供 longmemeval",
    )
    args = parser.parse_args(argv)

    load_evaluation_env()
    if os.getenv("MILVUS_URI") and not os.getenv("MEM2_MILVUS_URI"):
        os.environ["MEM2_MILVUS_URI"] = os.environ["MILVUS_URI"]
    config_file = Path(args.config).resolve()
    config = _load_config(config_file)
    benchmark = args.benchmark or str(config.get("benchmark", "longmemeval"))
    if benchmark != "longmemeval":
        raise RuntimeError("当前分支仅提供 longmemeval 测评")
    settings = config.get(benchmark, {})
    if not isinstance(settings, dict):
        raise RuntimeError(f"{benchmark} 配置必须是映射")

    from evaluation.longmemeval.entry import main as benchmark_main

    return benchmark_main(_longmemeval_args(config_file, settings))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("[evaluation] %s", exc)
        raise SystemExit(2) from None
