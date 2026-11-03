"""上下文打包（F09）的 LoCoMo 回放评测：复用一轮已落盘的召回，比较官方渲染与打包后的答题。

读 ``run.py`` 产物目录里的 ``test_recall_v5<conv>.jsonl``（每题召回的前 ``RECALL_TOP_K`` 条），
不重跑写入与召回，因此两组只差记忆块。每个 Top-K 下：

- ``baseline``：前 K 条按官方格式渲染（与 ``run.py`` 默认答题完全一致）；
- ``packed``：前 K 条经 :func:`jiuwen_memory.retrieval.context_pack.pack_context` 打包。

每组先并发跑完全部作答并计墙钟（含打包耗时），再单独判分（不计时）；重复 ``--runs`` 轮，
偶数轮两组先后倒置以抵消网关负载漂移。输出逐题判分、配对准确率差与 95% 置信区间、
记忆块 cl100k token 与降幅、每题墙钟时长（各轮平均）。

运行（需 ``evaluation/environment/.env`` 里的 MODEL_NAME / API_BASE / API_KEY）::

    python -m evaluation.locomo.context_pack_replay \\
        --run-dir evaluation/outputs/locomo/<RUN_ID>_<RUN_TAG> \\
        --data evaluation/datasets/locomo/locomo10.json --top-k 10,20,50

``--dry-run`` 只统计 token，不调用模型。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from evaluation.locomo import prompts
from evaluation.shared.env import load_evaluation_env, require_env
from evaluation.shared.tokens import cl100k_tokens
from jiuwen_memory.common.type_def import ChatMessage
from jiuwen_memory.retrieval.context_pack import PackMemory, pack_context, render_memories

logger = logging.getLogger("context_pack_replay")

_DATE_FMT = "%I:%M %p on %d %B, %Y"
_ARMS = ("baseline", "packed")


@dataclass(frozen=True)
class _Item:
    conv: int
    qa_idx: int
    category: int
    question: str
    gold: str
    reference_date: str
    memories: tuple[PackMemory, ...]


# ---------------------------------------------------------------- 题集（与 run.py 同口径）


def _replace_speaker_names(text: str, speaker_a: str, speaker_b: str) -> str:
    replacements = sorted(
        ((speaker_a, "user"), (speaker_b, "assistant")), key=lambda x: len(x[0]), reverse=True
    )
    for speaker, role in replacements:
        if speaker:
            text = text.replace(speaker, role)
    return text


def _infer_reference_date(conv: dict) -> str:
    conv_data = conv.get("conversation", {})
    for key in sorted(conv_data):
        if key.startswith("session_") and key.endswith("_date_time"):
            try:
                return datetime.strptime(conv_data[key].strip(), _DATE_FMT).strftime("%B %d, %Y")
            except (ValueError, AttributeError):
                continue
    return "2023"


def _parse_judge_json(content: str) -> str:
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            stop = end + 1
            label = str(json.loads(text[start:stop]).get("label", "")).strip().upper()
            if label:
                return label
        except json.JSONDecodeError:
            pass
    if "CORRECT" in text.upper() and "WRONG" not in text.upper():
        return "CORRECT"
    return "WRONG" if "WRONG" in text.upper() else ""


def load_items(run_dir: Path, data: list[dict], convs: list[int]) -> list[_Item]:
    """同一题有多条召回记录（RESUME 续跑追加）时取最后一条。"""
    items: dict[tuple[int, int], _Item] = {}
    for c in convs:
        conv = data[c]
        speaker_a = conv["conversation"].get("speaker_a", "")
        speaker_b = conv["conversation"].get("speaker_b", "")
        reference_date = _infer_reference_date(conv)
        with (run_dir / f"test_recall_v5{c}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                qa = conv["qa"][rec["qa_idx"]]
                category = qa.get("category", 0)
                gold = prompts.preprocess_answer(category, str(qa.get("answer", "")))
                memories = tuple(
                    PackMemory(m["content"], m.get("created_at", ""), float(m.get("score", 0.0)))
                    for m in rec["recalled_memories"]
                )
                items[(c, rec["qa_idx"])] = _Item(
                    conv=c,
                    qa_idx=rec["qa_idx"],
                    category=category,
                    question=rec["query"],
                    gold=_replace_speaker_names(gold, speaker_a, speaker_b),
                    reference_date=reference_date,
                    memories=memories,
                )
    return list(items.values())


# ---------------------------------------------------------------- 记忆块


def build_block(item: _Item, k: int, arm: str, ratio: float) -> tuple[str, dict]:
    top = item.memories[:k]
    if arm == "baseline" or not top:
        text = render_memories(top) if top else "(No relevant memories found)"
        return text, {"tokens": cl100k_tokens(text)}
    packed = pack_context(item.question, top, budget_ratio=ratio, count_tokens=cl100k_tokens)
    return packed.text, {
        "tokens": packed.tokens,
        "strategy": packed.strategy.value,
        "kept": len(packed.kept),
        "telegraphed": len(packed.telegraphed),
        "dropped": len(packed.dropped),
    }


# ---------------------------------------------------------------- 作答与判分


class _Llm:
    def __init__(self) -> None:
        from jiuwen_memory.common.llm.llm_impl.openai_llm import OpenAILLM

        env = require_env("MODEL_NAME", "API_BASE", "API_KEY")
        self._llm = OpenAILLM(
            model_name=env["MODEL_NAME"],
            base_url=env["API_BASE"],
            api_key=env["API_KEY"],
            timeout=float(os.getenv("LLM_TIMEOUT", "120")),
            max_retries=6,
        )
        disable = os.getenv("DISABLE_THINKING", "0").strip().lower() in ("1", "true", "yes", "on")
        self._extra = (
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if disable else {}
        )

    def answer(self, prompt: str) -> str:
        return self._llm.chat(
            [ChatMessage(role="user", content=prompt)],
            temperature=0.01,
            max_tokens=2048,
            **self._extra,
        )

    def judge(self, item: _Item, response: str) -> str:
        verdict = self._llm.chat(
            [
                ChatMessage(role="system", content=prompts.JUDGE_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=prompts.get_judge_prompt(
                        item.category, item.question, item.gold, response
                    ),
                ),
            ],
            temperature=0.0,
            max_tokens=512,
            **self._extra,
        )
        return _parse_judge_json(verdict)


def _run_arm(
    llm: _Llm, items: list[_Item], k: int, arm: str, ratio: float, concurrency: int, out: Path
) -> dict:
    def answer(item: _Item) -> dict:
        block, stats = build_block(item, k, arm, ratio)
        prompt = prompts.get_answer_generation_prompt(
            item.question, [], item.reference_date, memories_block=block
        )
        return {"item": item, "stats": stats, "response": llm.answer(prompt)}

    def judge(part: dict) -> dict:
        item = part["item"]
        return {
            "conv": item.conv,
            "qa_idx": item.qa_idx,
            "category": item.category,
            "response": part["response"],
            "label": llm.judge(item, part["response"]),
            **part["stats"],
        }

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        parts = list(ex.map(_guard(answer), items))
    wall = time.perf_counter() - started
    answered = [p for p in parts if p is not None]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        rows = [r for r in ex.map(_guard(judge), answered) if r is not None]
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("top%d %s: %d/%d judged, %.1fs answer wall", k, arm, len(rows), len(items), wall)
    return {
        "rows": {(r["conv"], r["qa_idx"]): r for r in rows},
        "wall_s_per_question": wall / max(len(answered), 1),
        "failures": len(items) - len(rows),
    }


def _guard(fn):
    def wrapped(arg):
        try:
            return fn(arg)
        except Exception as exc:  # noqa: BLE001 - counted as a failure and scored wrong.
            logger.warning("call failed: %s: %s", type(exc).__name__, str(exc)[:160])
            return None

    return wrapped


# ---------------------------------------------------------------- 汇总


def _token_stats(items: list[_Item], k: int, ratio: float) -> dict:
    full = packed = 0
    started = time.perf_counter()
    for item in items:
        packed += build_block(item, k, "packed", ratio)[1]["tokens"]
    pack_ms = (time.perf_counter() - started) * 1000 / max(len(items), 1)
    for item in items:
        full += build_block(item, k, "baseline", ratio)[1]["tokens"]
    n = max(len(items), 1)
    return {
        "tokens_baseline": round(full / n, 1),
        "tokens_packed": round(packed / n, 1),
        "token_cut": round(1 - packed / max(full, 1), 4),
        "pack_ms_per_question": round(pack_ms, 2),
    }


def _paired(items: list[_Item], results: dict[tuple[int, str], dict], runs: int) -> dict:
    """每题先对各轮的配对差取平均，再按题算 95% CI（同一题多轮不当作独立样本）。"""
    per_question: list[float] = []
    correct = {arm: 0 for arm in _ARMS}
    for item in items:
        key = (item.conv, item.qa_idx)
        diffs = []
        for run in range(1, runs + 1):
            ok = {}
            for arm in _ARMS:
                row = results[(run, arm)]["rows"].get(key)
                ok[arm] = int(row is not None and row.get("label") == "CORRECT")
                correct[arm] += ok[arm]
            diffs.append(ok.get("packed", 0) - ok.get("baseline", 0))
        per_question.append(sum(diffs) / runs)
    n = len(per_question)
    mean = sum(per_question) / n
    var = sum((d - mean) ** 2 for d in per_question) / max(n - 1, 1)
    half = 1.96 * math.sqrt(var / n)
    return {
        "questions": n,
        "runs": runs,
        "acc_baseline": round(100 * correct["baseline"] / (n * runs), 2),
        "acc_packed": round(100 * correct["packed"] / (n * runs), 2),
        "diff_pp": round(100 * mean, 2),
        "ci95_pp": [round(100 * (mean - half), 2), round(100 * (mean + half), 2)],
        **{
            f"wall_s_per_question_{arm}": round(
                sum(results[(r, arm)]["wall_s_per_question"] for r in range(1, runs + 1)) / runs,
                3,
            )
            for arm in _ARMS
        },
        **{
            f"failures_{arm}": sum(results[(r, arm)]["failures"] for r in range(1, runs + 1))
            for arm in _ARMS
        },
    }


def _markdown(summary: dict[str, dict], dry_run: bool) -> str:
    if dry_run:
        lines = [
            "| Top-K | 原 token | 打包后 token | 降幅 | 打包耗时 ms/题 |",
            "|---|---|---|---|---|",
        ]
        for k, s in summary.items():
            lines.append(
                f"| {k} | {s['tokens_baseline']:.0f} | {s['tokens_packed']:.0f} | "
                f"{100 * s['token_cut']:.1f}% | {s['pack_ms_per_question']:.1f} |"
            )
        return "\n".join(lines)
    lines = [
        "| Top-K | 基线 | 打包 | 差（95% CI） | 原 token | 打包后 token | 降幅 "
        "| 每题时长（基线 → 打包） |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for k, s in summary.items():
        lo, hi = s["ci95_pp"]
        lines.append(
            f"| {k} | {s['acc_baseline']:.2f} | {s['acc_packed']:.2f} | "
            f"{s['diff_pp']:+.2f}（{lo:+.2f}, {hi:+.2f}） | {s['tokens_baseline']:.0f} | "
            f"{s['tokens_packed']:.0f} | {100 * s['token_cut']:.1f}% | "
            f"{s['wall_s_per_question_baseline']:.3f} → {s['wall_s_per_question_packed']:.3f} 秒 |"
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoCoMo 上下文打包（F09）回放评测")
    parser.add_argument(
        "--run-dir", required=True, help="run.py 产物目录（含 test_recall_v5*.jsonl）"
    )
    parser.add_argument("--data", required=True, help="与该轮相同的 LoCoMo 数据文件")
    parser.add_argument("--conversation-ids", default="", help="逗号分隔；默认取目录里全部会话")
    parser.add_argument("--top-k", default="10,20,50")
    parser.add_argument("--ratio", type=float, default=0.8)
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--runs", type=int, default=2, help="每组重复轮数；偶数轮两组先后倒置")
    parser.add_argument("--max-questions", type=int, default=0, help="只跑前 N 题（冒烟）；0=全量")
    parser.add_argument("--output-dir", default="", help="默认 <run-dir>/context_pack_replay")
    parser.add_argument("--dry-run", action="store_true", help="只统计 token，不调用模型")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("agent_memory", "httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_evaluation_env()
    args = _build_parser().parse_args(argv)
    if args.runs < 1:
        raise RuntimeError("--runs 至少为 1")
    run_dir = Path(args.run_dir).resolve()
    with open(args.data, encoding="utf-8") as fh:
        data = json.load(fh)
    if args.conversation_ids:
        convs = [int(x) for x in args.conversation_ids.split(",")]
    else:
        convs = sorted(
            int(p.stem.removeprefix("test_recall_v5"))
            for p in run_dir.glob("test_recall_v5*.jsonl")
        )
    items = load_items(run_dir, data, convs)
    if args.max_questions > 0:
        items = items[: args.max_questions]
    if not items:
        raise RuntimeError(f"{run_dir} 下没有可回放的召回记录")
    out_dir = Path(args.output_dir or run_dir / "context_pack_replay").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("%d questions from conversations %s", len(items), convs)

    llm = None if args.dry_run else _Llm()
    summary: dict[str, dict] = {}
    for k in (int(x) for x in args.top_k.split(",")):
        stats = _token_stats(items, k, args.ratio)
        if llm is not None:
            results = {}
            for run in range(1, args.runs + 1):
                for arm in _ARMS if run % 2 else _ARMS[::-1]:
                    out = out_dir / f"top{k}_{arm}_r{run}.jsonl"
                    results[(run, arm)] = _run_arm(
                        llm, items, k, arm, args.ratio, args.concurrency, out
                    )
            stats = {**_paired(items, results, args.runs), **stats}
        summary[f"Top-{k}"] = stats

    meta = {
        "run_dir": str(run_dir),
        "conversations": convs,
        "ratio": args.ratio,
        "runs": args.runs,
        "concurrency": args.concurrency,
        "summary": summary,
    }
    (out_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    table = _markdown(summary, args.dry_run)
    (out_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    logger.info("summary:\n%s", table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
