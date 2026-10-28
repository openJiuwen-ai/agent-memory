"""MemoryAgentBench `factconsolidation` 独立评测（agent-memory）。

本目录自包含：**不修改** `evaluation/` 其它代码。流程对齐 MindMemOS：
add(context chunks) → eval → conflict_consolidate → eval（不重 add）→ 对比
`substring_exact_match`。

数据集读 `evaluation/datasets/memoryagentbench/<子集>.json`（由 `export_datasets.py`
从官方 parquet 导出，见该目录 README）；跑哪个子集由 `runner.data` 决定。

用法：
  .venv/bin/python -m evaluation.memoryagentbench.run --stage all
  .venv/bin/python -m evaluation.memoryagentbench.run --stage all \
      --data evaluation/datasets/memoryagentbench/factconsolidation_mh_32k.json
  .venv/bin/python -m evaluation.memoryagentbench.run --stage add --run-tag T --run-id ID
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import string
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import openai as _openai

logger = logging.getLogger(__name__)

EVAL_DIR = Path(__file__).resolve().parent
REPO = EVAL_DIR.parent.parent
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from evaluation.shared.env import load_evaluation_env  # noqa: E402

load_evaluation_env()

from evaluation.shared.config_loader import load_layer  # noqa: E402
from jiuwen_memory.api.memory_api_impl.assembly import assemble_runtime  # noqa: E402
from jiuwen_memory.common.llm.base import LlmProducer  # noqa: E402, F401
from jiuwen_memory.common.llm.llm_impl.openai_llm import OpenAILLM as _OpenAILLM  # noqa: E402
from jiuwen_memory.common.schema_property import is_schema_property_unit  # noqa: E402
from jiuwen_memory.common.security.request_context import new_request_context  # noqa: E402
from jiuwen_memory.common.security.types import (  # noqa: E402
    AuthContext,
    RequestSecurityContext,
    Surface,
)
from jiuwen_memory.common.type_def import (  # noqa: E402
    ChatMessage,
    Context,
    LifecycleState,
    Scope,
)
from jiuwen_memory.common.type_def.candidate import PredicateCandidate  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.construction import EvolveMode  # noqa: E402
from jiuwen_memory.retrieval.types import DisclosureLevel  # noqa: E402

# --- 关掉思维链 + 瞬态错误重试（与 evaluation/locomo/run.py 同款类级 patch）-----
# 1) glm-5.2 若不禁用 thinking，会把 token 花在 reasoning 上、content 返回空。
# 2) LLM/embedding 的 max_retries=0，一次 429/超时/5xx 就会让 add 失败或降级；
#    这里对 LLM 加 4 次退避重试（1/2/4s），覆盖引擎内 extractor 与脚本 answer。
# openai 在标准库分组内一并导入（顶部），此处只按其异常类型组瞬态集合。
_ORIG_CHAT = _OpenAILLM.chat
_TRANSIENT = (
    _openai.RateLimitError,
    _openai.APIConnectionError,
    _openai.APITimeoutError,
    _openai.InternalServerError,
)


def _chat_resilient(self, messages, **options):
    # glm 系列需要显式关闭思维链（否则 content 返回空）；OpenAI 兼容模型不认识该字段，
    # 只在模型名含 glm 时才注入，避免 gpt-4o-mini 之类收到未知参数。
    if "glm" in os.getenv("MODEL_NAME", "").lower():
        extra = options.get("extra_body")
        template = {"chat_template_kwargs": {"enable_thinking": False}}
        options["extra_body"] = {**template, **extra} if isinstance(extra, dict) else template
    for attempt in range(4):
        try:
            return _ORIG_CHAT(self, messages, **options)
        except _TRANSIENT as exc:
            if attempt == 3:
                logger.error("[llm] transient error retries exhausted: %s", type(exc).__name__)
                raise
            wait = min(2.0 ** attempt, 16.0)
            logger.warning(
                "[llm] transient %s, retry in %.0fs (%d/3)", type(exc).__name__, wait, attempt + 1
            )
            time.sleep(wait)
    logger.error("[llm] retry loop exited unexpectedly")
    return _ORIG_CHAT(self, messages, **options)


_OpenAILLM.chat = _chat_resilient

# --- embedding 瞬态错误重试：OpenAIEmbedder._embed_batch 超时/连接错误直接 raise ---
from jiuwen_memory.common.embedder.embedder_impl.openai_embedder import (  # noqa: E402
    OpenAIEmbedder as _OpenAIEmbedder,
)

_ORIG_EMBED = _OpenAIEmbedder.embed


def _embed_resilient(self, texts):
    for attempt in range(4):
        try:
            return _ORIG_EMBED(self, texts)
        except _TRANSIENT as exc:
            if attempt == 3:
                logger.error("[embed] transient error retries exhausted: %s", type(exc).__name__)
                raise
            wait = min(2.0 ** attempt, 16.0)
            logger.warning(
                "[embed] transient %s, retry in %.0fs (%d/3)", type(exc).__name__, wait, attempt + 1
            )
            time.sleep(wait)
    logger.error("[embed] retry loop exited unexpectedly")
    return _ORIG_EMBED(self, texts)


_OpenAIEmbedder.embed = _embed_resilient

# --- 抬高 ConflictConsolidator 的输出上限 --------------------------------------
# 该值是巩固阶段两次 LLM 调用（关系检测 / 动作规划）的**输出** token 上限：每次要为
# 一个 (entity, property) 簇产一份 JSON，簇大（数十条）时会被截断 → 解析失败 →
# 整簇留待重试（静默的部分巩固）。装配处（schema_orchestrating_evolver._build）
# 不传该参数，只能用类默认 2048，故在此覆盖；与 evaluation/locomo/consolidate.py 同款补丁。
from jiuwen_memory.construction.evolver_impl.conflict_consolidator import (  # noqa: E402
    ConflictConsolidator as _ConflictConsolidator,
)

_CONSOLIDATE_OPTS = {"max_tokens": 8192}
_ORIG_CC_INIT = _ConflictConsolidator.__init__


def _cc_init_patched(self, **kwargs):
    kwargs.setdefault("max_tokens", _CONSOLIDATE_OPTS["max_tokens"])
    _ORIG_CC_INIT(self, **kwargs)


_ConflictConsolidator.__init__ = _cc_init_patched

SUB_DATASET = "factconsolidation_mh_6k"
DEFAULT_DATA = REPO / "evaluation" / "datasets" / "memoryagentbench" / "factconsolidation_mh_6k.json"
DEFAULT_CONFIG = EVAL_DIR / "config.yml"

_MEMORIZE = (
    "Dialogue between User and Assistant {time_stamp}\n"
    "<User> The following context is the facts I have learned: \n{context}\n"
    " <Assistant> I have learned the facts and I will answer the question you ask."
)
_QUERY = (
    "Pretend you are a knowledge management system. Each fact in the Archival Memory "
    "is provided with a serial number at the beginning, and the newer fact has larger "
    "serial number. \n You need to solve the conflicts of facts in the Archival Memory "
    "by finding the newest fact with larger serial number. You need to answer a question "
    "based on this rule. You should give a very concise answer without saying other words "
    "for the question **only** from the knowledge pool you have memorized rather than the "
    "real facts in real world. \n\nFor example:\n\n [Archival Memory] \n\n "
    "Question: Based on the Archival Memory, what is the name of the current president of Russia? \n"
    "Answer: Donald Trump \n\n Now Answer the Question: Based on the  Archival Memory, "
    "{question} \nAnswer:"
)
_ANSWER_SYSTEM = "You are a helpful AI. Answer the question based on query and memories.\n{memories}\n"


# ---------------------------------------------------------------------------
# 评分（照搬 MindMemOS 官方口径）
# ---------------------------------------------------------------------------


def normalize_answer(text: str) -> str:
    text = (text or "").lower()
    text = "".join(char for char in text if char not in string.punctuation)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def parse_output(output_text: str, answer_prefix: str = "Answer:") -> str:
    for pattern in (
        re.compile(f"(?:{re.escape(answer_prefix)})(.*)(?:\n|$)", flags=re.IGNORECASE),
        re.compile(r"(?:^)(.*)(?:\n|$)"),
    ):
        match = pattern.search(output_text or "")
        if match:
            extracted = match[1].strip()
            return re.sub(f"^{re.escape(answer_prefix)}", "", extracted, flags=re.IGNORECASE).strip()
    return ""


def _flat(answers) -> list[str]:
    values = answers if isinstance(answers, list) else [answers]
    out: list[str] = []
    for value in values:
        if isinstance(value, list):
            out.extend(str(v) for v in value)
        else:
            out.append(str(value))
    return out


def substring_exact_match(prediction: str, answers) -> float:
    golds = _flat(answers)
    if not golds:
        return 0.0
    pred = normalize_answer(prediction)
    parsed = normalize_answer(parse_output(prediction))
    return float(
        any(normalize_answer(g) in t for g in golds for t in (pred, parsed))
    )


# ---------------------------------------------------------------------------
# 数据 / 切块 / prompt
# ---------------------------------------------------------------------------


def chunk_context(text: str, max_chars: int = 16000) -> list[str]:
    """按行累积到 ~max_chars 一块（factconsolidation 每行一条事实）。"""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        if buf and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [text]


def memorize_message(chunk: str) -> str:
    return _MEMORIZE.format(
        time_stamp=time.strftime("%Y-%m-%d %H:%M:%S"), context=chunk
    )


def query_message(question: str) -> str:
    return _QUERY.format(question=question)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


class MabRunner:
    def __init__(self, config_path: Path, *, top_k: int, chunk_chars: int = 8000) -> None:
        self._payload = dict(load_layer(str(config_path)).get("memory_api"))
        self._rt = assemble_runtime(config=Config(self._payload))
        self._api = self._rt.api
        # 模块级的 OpenAILLM 已被 _chat_resilient patch，直接复用同一对象。
        self._llm = _OpenAILLM(
            model_name=os.getenv("MODEL_NAME", ""),
            base_url=os.getenv("API_BASE", ""),
            api_key=os.getenv("API_KEY", ""),
            default_temperature=0.01,
            default_max_tokens=4096,
        )
        self._top_k = top_k
        self._chunk_chars = chunk_chars

    @staticmethod
    def security(scope: Scope) -> RequestSecurityContext:
        # 必须经受控入口生成：mem2.0 起 PEP 校验 ``has_valid_origin()``（HMAC 绑定
        # auth + 上下文），直接构造的 RequestSecurityContext 没有签名，写操作一律
        # 被拒（PermissionDeniedError: write）。Surface.INTERNAL 表示进程内调用。
        return new_request_context(AuthContext(actor=scope), surface=Surface.INTERNAL)

    # -- add ---------------------------------------------------------------
    def add(self, scope: Scope, context: str) -> dict:
        """逐 chunk 写入；返回写入统计（失败/降级可见，不再只数 chunk）。"""
        chunks = chunk_context(context, max_chars=self._chunk_chars)
        ok = 0
        property_units = 0
        failed: list[dict] = []
        degraded: list[int] = []
        for idx, chunk in enumerate(chunks):
            try:
                units = self._api.add(
                    memorize_message(chunk),
                    scope,
                    security=self.security(scope),
                    system_metadata={"infer": "true"},
                ) or []
                props = sum(1 for unit in units if is_schema_property_unit(unit))
                property_units += props
                ok += 1
                if props == 0:
                    # 抽取降级：只写了 source，没有 property 记忆。
                    degraded.append(idx + 1)
                logger.info(
                    "[add] chunk %d/%d ok (units=%d, property=%d)",
                    idx + 1, len(chunks), len(units), props,
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {"index": idx + 1, "error": type(exc).__name__, "detail": str(exc)[:200]}
                )
                logger.error("[add] chunk %d/%d FAILED: %s", idx + 1, len(chunks), exc)
        return {
            "chunks_total": len(chunks),
            "chunks_ok": ok,
            "chunks_failed": len(failed),
            "failed": failed,
            "degraded": degraded,
            "property_units": property_units,
        }

    # -- eval --------------------------------------------------------------
    def eval(self, scope: Scope, questions, answers, tag: str) -> dict:
        """逐题 recall + 生成 + 打分。

        瞬态错误（限流/连接/超时/5xx）**不计入分母**（对齐 locomo 口径），
        其余异常仍计为错题，但把异常类型写进 record 便于排查。
        """
        correct = 0
        attempted = 0
        skipped = 0
        records = []
        for i, (question, gold) in enumerate(zip(questions, answers), 1):
            query = query_message(question)
            try:
                res = self._api.search(
                    query,
                    Context(scope),
                    security=self.security(scope),
                    top_k=self._top_k,
                    disclosure=DisclosureLevel.L2,
                )
                memories = [item.content for item in res.items]
            except _TRANSIENT as exc:
                skipped += 1
                logger.warning("[eval] Q%d search skipped (%s)", i, type(exc).__name__)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("[eval] Q%d search failed: %s", i, exc)
                memories = []
            messages = [
                ChatMessage(role="system", content=_ANSWER_SYSTEM.format(
                    memories="\n".join(f"- {m}" for m in memories))),
                ChatMessage(role="user", content=query),
            ]
            error = ""
            try:
                answer = self._llm.chat(messages, temperature=0, max_tokens=256)
            except _TRANSIENT as exc:
                skipped += 1
                logger.warning("[eval] Q%d answer skipped (%s)", i, type(exc).__name__)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("[eval] Q%d answer failed: %s", i, exc)
                answer = ""
                error = type(exc).__name__
            attempted += 1
            score = substring_exact_match(answer, gold)
            correct += int(score)
            records.append({
                "i": i, "question": question, "answers": gold, "answer": answer,
                "score": score, "n_memories": len(memories), "error": error,
            })
            if attempted % 10 == 0 or i == len(questions):
                logger.info(
                    "[eval:%s] %d/%d acc=%.3f skipped=%d",
                    tag, i, len(questions),
                    correct / max(1, attempted), skipped,
                )
        return {
            "acc": correct / max(1, attempted),
            "correct": correct,
            "attempted": attempted,
            "skipped": skipped,
            "total": len(questions),
            "records": records,
        }

    # -- consolidate -------------------------------------------------------
    def consolidate(self, scope: Scope) -> dict:
        before = self._count_active_props(scope)
        self._api.evolve(
            scope, EvolveMode.CONFLICT_CONSOLIDATE,
            candidate=PredicateCandidate(),
            security=self.security(scope),
        )
        after = self._count_active_props(scope)
        return {"active_props_before": before, "active_props_after": after}

    def _count_active_props(self, scope: Scope) -> int:
        items, offset = [], 0
        while True:
            res = self._api.list(scope, security=self.security(scope), offset=offset, limit=200)
            items.extend(res.items)
            if len(items) >= res.count or not res.items:
                break
            offset += 200
        return sum(
            1 for u in items
            if is_schema_property_unit(u) and u.lifecycle is LifecycleState.ACTIVE
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MemoryAgentBench factconsolidation")
    parser.add_argument("--stage", default="all",
                        choices=["all", "add", "eval", "eval-after", "consolidate", "finalize"])
    parser.add_argument("--data", default=None, help="默认取 config.yml 的 runner.data")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--top-k", type=int, default=None, help="默认取 config.yml 的 runner.top_k")
    parser.add_argument(
        "--chunk-chars", type=int, default=None, help="默认取 config.yml 的 runner.chunk_chars"
    )
    parser.add_argument("--max-questions", type=int, default=0, help="0=全部")
    parser.add_argument(
        "--consolidate-max-tokens",
        type=int,
        default=None,
        help="巩固阶段每次 LLM 调用的输出上限；默认 8192",
    )
    parser.add_argument("--run-tag", default=None, help="默认按数据集名推导（mab-mh6k / mab-sh6k）")
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--output-root", default=str(REPO / "evaluation" / "outputs" / "memoryagentbench")
    )
    args = parser.parse_args(argv)

    # 运行参数优先级：命令行 > config.yml 的 runner 段 > 代码默认值。
    # 这样切换数据集 / 切块大小只改配置，不必每次重敲命令。
    runner_cfg = dict(load_layer(args.config).get("runner") or {})
    if args.data is None:
        configured = runner_cfg.get("data")
        args.data = str((REPO / configured).resolve()) if configured else str(DEFAULT_DATA)
    if args.top_k is None:
        args.top_k = int(runner_cfg.get("top_k", 50))
    if args.chunk_chars is None:
        args.chunk_chars = int(runner_cfg.get("chunk_chars", 8000))
    # 必须在 MabRunner 装配（构造 ConflictConsolidator）之前生效。
    if args.consolidate_max_tokens is not None:
        _CONSOLIDATE_OPTS["max_tokens"] = args.consolidate_max_tokens

    sample = json.loads(Path(args.data).read_text())[0]
    questions = sample["questions"]
    answers = sample["answers"]
    if args.max_questions > 0:
        questions, answers = questions[: args.max_questions], answers[: args.max_questions]

    run_id = args.run_id or f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    # 子数据集名从数据文件的 metadata.source 取（sh_6k / mh_6k …），用于 scope 标签与报告。
    sub_dataset = str(sample.get("metadata", {}).get("source") or SUB_DATASET)
    # run_tag 未指定时按子数据集推导：factconsolidation_mh_6k → mab-mh6k
    if args.run_tag is None:
        short = sub_dataset.replace("factconsolidation_", "").replace("_", "")
        args.run_tag = f"mab-{short}"
    scope = Scope(org=f"{args.run_tag}-{run_id}", user=f"context_{sub_dataset}")
    out_dir = Path(args.output_root) / f"{run_id}_{args.run_tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== MAB %s | stage=%s | QA=%d | scope_org=%s ===", sub_dataset, args.stage, len(questions), scope.org)
    runner = MabRunner(Path(args.config), top_k=args.top_k, chunk_chars=args.chunk_chars)

    report: dict = {"sub_dataset": sub_dataset, "stage": args.stage,
                    "scope_org": scope.org, "n_questions": len(questions)}

    if args.stage in ("all", "add"):
        t0 = time.time()
        add_stats = runner.add(scope, sample["context"])
        add_stats["elapsed_s"] = round(time.time() - t0, 1)
        report["add"] = add_stats
        logger.info(
            "=== add done: %d/%d chunks, property=%d, degraded=%d, failed=%d, %.1fs ===",
            add_stats["chunks_ok"], add_stats["chunks_total"], add_stats["property_units"],
            len(add_stats["degraded"]), add_stats["chunks_failed"], add_stats["elapsed_s"],
        )

    if args.stage in ("all", "eval"):
        logger.info("=== eval BEFORE ===")
        report["eval_before"] = runner.eval(scope, questions, answers, "before")

    if args.stage in ("all", "consolidate", "finalize"):
        logger.info("=== consolidate (max_tokens=%d) ===", _CONSOLIDATE_OPTS["max_tokens"])
        report["consolidate"] = runner.consolidate(scope)
        logger.info("  %s", report["consolidate"])

    if args.stage in ("all", "finalize", "eval-after"):
        logger.info("=== eval AFTER ===")
        report["eval_after"] = runner.eval(scope, questions, answers, "after")

    out = out_dir / f"report_{args.stage}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("=== 报告: %s ===", out)
    if args.stage == "all" and "eval_before" in report and "eval_after" in report:
        before, after = report["eval_before"], report["eval_after"]
        logger.info(
            "substring_exact_match  BEFORE=%.4f  AFTER=%.4f  delta=%+.4f  skipped=%d/%d",
            before["acc"], after["acc"], after["acc"] - before["acc"],
            before["skipped"], after["skipped"],
        )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
