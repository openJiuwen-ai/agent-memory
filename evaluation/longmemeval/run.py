"""LongMemEval 端到端测评入口。

本文件保留 SSH 机器上的测评链路：按 dialogue turn 写入（超长轮次按句回退）、
Oracle session、infer 抽取、Top-200 检索、多 cutoff 生成答案并用 LongMemEval
AnswerJudge 判分。运行适配仅限路径、环境变量和单题选择。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from evaluation.shared.env import load_evaluation_env

load_evaluation_env()

from evaluation.longmemeval.adapter import LongMemEvalDataset  # noqa: E402
from evaluation.longmemeval.metrics import mem0_longmemeval_prompt as _prompts  # noqa: E402
from evaluation.longmemeval.metrics.ir_metrics import ir_metrics  # noqa: E402
from evaluation.longmemeval.metrics.llm_judge import (  # noqa: E402
    LONGMEMEVAL_PROMPT_PROFILE,
    LLMJudge,
    openai_chat,
)
from evaluation.longmemeval.metrics.perf_metrics import perf_metrics  # noqa: E402
from evaluation.longmemeval.metrics.qa_metrics import qa_accuracy  # noqa: E402
from evaluation.longmemeval.report import to_json, to_markdown  # noqa: E402
from evaluation.longmemeval.runner import Runner  # noqa: E402
from evaluation.shared.config_loader import load_layer  # noqa: E402
from jiuwen_memory.config.config import Config  # noqa: E402

logger = logging.getLogger(__name__)

_EVALUATION_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_DATA = _EVALUATION_DIR / "datasets" / "longmemeval" / "longmemeval_oracle.json"
_DEFAULT_CONFIG = Path(__file__).with_name("config.yml")
_DEFAULT_OUTPUT_ROOT = _EVALUATION_DIR / "outputs" / "longmemeval"


def _parse_samples(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    try:
        values = [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError("--samples 必须是逗号分隔的整数下标") from exc
    if not values:
        raise ValueError("--samples 不能为空")
    return values


def _parse_cutoffs(raw: str) -> list[int]:
    try:
        cutoffs = [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError("--answer-cutoffs 必须是逗号分隔的整数") from exc
    if not cutoffs:
        raise ValueError("--answer-cutoffs 不能为空")
    return cutoffs


def _question_samples(path: Path, question_ids: list[str]) -> list[int]:
    with path.open("r", encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise ValueError("LongMemEval 数据根节点必须是数组")
    index_by_id = {
        str(row.get("question_id")): index
        for index, row in enumerate(rows)
        if isinstance(row, dict) and row.get("question_id") is not None
    }
    missing = [question_id for question_id in question_ids if question_id not in index_by_id]
    if missing:
        raise ValueError("找不到 question_id: " + ", ".join(missing))
    return [index_by_id[question_id] for question_id in question_ids]


def _load_memory_config(path: Path) -> tuple[Config, dict]:
    raw = load_layer(str(path))
    payload = raw.get("memory_api", raw) if isinstance(raw, dict) else raw
    if not isinstance(payload, dict):
        raise ValueError("配置根节点（或 memory_api）必须是映射")
    return Config.from_dict(payload), payload


def _default_output_dir() -> Path:
    return _DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LongMemEval 单题/小批量端到端测评")
    parser.add_argument("--data", default=str(_DEFAULT_DATA), help="LongMemEval JSON 数据文件")
    parser.add_argument("--samples", default=None, help="逗号分隔的样本数组下标")
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="按 question_id 选择，可重复；与 --samples 互斥",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=int(os.getenv("LONGMEMEVAL_MAX_QUESTIONS", "1")),
        help="最多运行题数；0 表示不限制（默认 1，适合功能冒烟）",
    )
    parser.add_argument("--scope-org", default="longmemeval-0804")
    parser.add_argument(
        "--granularity",
        choices=("turn", "dialogue_turn", "session"),
        default="dialogue_turn",
    )
    parser.add_argument("--dialogue-turn-max-chars", type=int, default=4096)
    parser.add_argument("--recall-top-k", "--top-k", dest="recall_top_k", type=int, default=200)
    parser.add_argument("--answer-cutoff", type=int, default=50)
    parser.add_argument(
        "--answer-cutoffs",
        default="50,10,20",
        help="按 SSH 测评调用顺序执行的答案上下文 cutoff",
    )
    parser.add_argument(
        "--oracle-sessions",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--infer", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Mem2.0 两级 YAML 配置")
    parser.add_argument("--output-dir", default=None, help="本轮产物目录")
    parser.add_argument("--json", default=None, help="机器可读结果路径")
    parser.add_argument("--artifact-dir", default=None, help="单题无损审计产物目录")
    parser.add_argument(
        "--judge-model",
        default=os.getenv("JUDGE_MODEL") or os.getenv("MODEL_NAME"),
    )
    parser.add_argument(
        "--judge-base-url",
        default=os.getenv("JUDGE_BASE_URL") or os.getenv("API_BASE"),
    )
    parser.add_argument(
        "--judge-api-key",
        default=os.getenv("JUDGE_API_KEY") or os.getenv("API_KEY"),
    )
    parser.add_argument("--answer-temperature", type=float, default=0.0)
    parser.add_argument(
        "--answer-max-tokens",
        type=int,
        default=int(os.getenv("LONGMEMEVAL_ANSWER_MAX_TOKENS", "16384")),
    )
    parser.add_argument("--judge-temperature", type=float, default=0.0)
    parser.add_argument(
        "--judge-max-tokens",
        type=int,
        default=int(os.getenv("LONGMEMEVAL_JUDGE_MAX_TOKENS", "4096")),
    )
    parser.add_argument("--llm-timeout", type=float, default=900.0)
    parser.add_argument("--judge-strict", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        data_path = Path(args.data).resolve()
        config_path = Path(args.config).resolve()
        if not data_path.is_file():
            raise ValueError(f"数据文件不存在: {data_path}")
        if not config_path.is_file():
            raise ValueError(f"配置文件不存在: {config_path}")
        if args.samples and args.question_id:
            raise ValueError("--samples 与 --question-id 不能同时使用")
        if args.max_questions < 0:
            raise ValueError("--max-questions 不能小于 0")

        samples = _parse_samples(args.samples)
        if args.question_id:
            samples = _question_samples(data_path, args.question_id)
        max_questions = None if args.max_questions == 0 else args.max_questions
        cutoffs = _parse_cutoffs(args.answer_cutoffs)

        output_dir = Path(args.output_dir).resolve() if args.output_dir else _default_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = Path(args.json).resolve() if args.json else output_dir / "result.json"
        one_question = (samples is not None and len(samples) == 1) or (
            samples is None and max_questions == 1
        )
        artifact_dir = (
            Path(args.artifact_dir).resolve()
            if args.artifact_dir
            else output_dir / "artifacts" if one_question else None
        )

        config, memory_config = _load_memory_config(config_path)
        dataset = LongMemEvalDataset(
            str(data_path),
            samples=samples,
            max_questions=max_questions,
            scope_org=args.scope_org,
            granularity=args.granularity,
            top_k=args.recall_top_k,
            answer_cutoff=args.answer_cutoff,
            answer_cutoffs=cutoffs,
            infer=args.infer,
            oracle_sessions=args.oracle_sessions,
            dialogue_turn_max_chars=args.dialogue_turn_max_chars,
        )
    except (OSError, ValueError, IndexError) as exc:
        logger.error("%s", exc)
        return 2

    judge = None
    if args.judge_model and args.judge_base_url and args.judge_api_key:
        answer_chat = openai_chat(
            args.judge_base_url,
            args.judge_model,
            args.judge_api_key,
            temperature=args.answer_temperature,
            max_tokens=args.answer_max_tokens,
            timeout=args.llm_timeout,
            audit_category="answer",
        )
        judge_chat = openai_chat(
            args.judge_base_url,
            args.judge_model,
            args.judge_api_key,
            temperature=args.judge_temperature,
            max_tokens=args.judge_max_tokens,
            timeout=args.llm_timeout,
            audit_category="judge",
        )
        judge = LLMJudge(
            chat=answer_chat,
            judge_chat=judge_chat,
            strict=args.judge_strict,
            prompt_profile=LONGMEMEVAL_PROMPT_PROFILE,
        )

    runner = Runner(
        [
            ir_metrics(ks=(10, 40, 50)),
            perf_metrics(),
            qa_accuracy(judge=judge, concurrency=args.concurrency),
        ]
    )
    result = runner.run(
        dataset,
        config=config,
        concurrency=args.concurrency,
        artifact_dir=artifact_dir,
    )
    logger.info(to_markdown(result))

    payload = to_json(result)
    payload["evaluation_protocol"] = {
        "version": "0804",
        "context_mode": "oracle_sessions" if args.oracle_sessions else "full_haystack",
        "granularity": args.granularity,
        "dialogue_turn_max_chars": args.dialogue_turn_max_chars,
        "oversized_dialogue_turn": "sentence_fallback_with_bounded_prior_context",
        "extraction_fidelity_mode": False,
        "retain_source": False,
        "chunk_size": memory_config.get("globals", {}).get("chunk_size"),
        "recall_top_k": args.recall_top_k,
        "answer_cutoff": args.answer_cutoff,
        "answer_cutoffs": cutoffs,
        "infer": args.infer,
        "concurrency": args.concurrency,
        "raw_fallback": False,
        "raw_always": False,
        "date_prefix": False,
        "disable_thinking_injection": False,
        "answer_prompt_profile": LONGMEMEVAL_PROMPT_PROFILE,
        "answer_prompt_sha256": hashlib.sha256(
            _prompts.ANSWER_GENERATION_PROMPT.encode()
        ).hexdigest(),
        "judge_prompt_profile": LONGMEMEVAL_PROMPT_PROFILE,
        "judge_prompt_sha256": hashlib.sha256(_prompts.JUDGE_PROMPT.encode()).hexdigest(),
        "answer_temperature": args.answer_temperature,
        "answer_max_tokens": args.answer_max_tokens,
        "judge_temperature": args.judge_temperature,
        "judge_max_tokens": args.judge_max_tokens,
        "llm_timeout": args.llm_timeout,
        "artifact_schema": "longmemeval_lossless_evidence_v1",
    }
    if judge is not None:
        payload["qa_records"] = judge.records
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info("\n[result] %s", json_path)
    if judge is None:
        logger.info("\n[note] 未配置 AnswerJudge，仅输出 IR/性能指标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
