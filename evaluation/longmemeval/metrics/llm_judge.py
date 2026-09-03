"""可插拔 LLM judge——端到端 QA 的「答案合成 + 判分」实现。

端到端评测分两步 LLM 调用：① 从召回的记忆合成答案；② LLM-as-judge
比对参考答案给 CORRECT/WRONG。本模块把两步封装成一个符合
:data:`evaluation.longmemeval.metrics.qa_metrics.JudgeFn` 的可调用，注入即用：

    from evaluation.longmemeval.metrics.llm_judge import LLMJudge, openai_chat
    judge = LLMJudge(chat=openai_chat(base_url=..., model=..., api_key=...))
    runner = Runner([ir_metrics(), perf_metrics(), qa_accuracy(judge=judge)])

``chat`` 是 ``(system, user) -> str`` 的薄抽象——provider 无关：``openai_chat`` 给一个
OpenAI 兼容实现（智谱/豆包/OpenAI 等填各自 ``base_url`` 即可），也可注入任意自定义
实现（便于离线测试用确定性桩）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from evaluation.longmemeval.metrics.mem0_longmemeval_prompt import (
    get_answer_generation_prompt as get_longmemeval_answer_prompt,
)
from evaluation.longmemeval.metrics.mem0_longmemeval_prompt import (
    get_judge_prompt as get_longmemeval_judge_prompt,
)

# (system_prompt, user_prompt) -> 模型输出文本
ChatFn = Callable[[str, str], str]

GENERIC_PROMPT_PROFILE = "generic"
LONGMEMEVAL_PROMPT_PROFILE = "mem0_longmemeval"
_PROMPT_PROFILES = {GENERIC_PROMPT_PROFILE, LONGMEMEVAL_PROMPT_PROFILE}

_ANSWER_SYS = (
    "You answer questions strictly from the provided memories. "
    "If the memories do not contain the answer, reply exactly: I don't know."
)

_JUDGE_SYS = (
    "You are a strict grader for a long-term-memory QA benchmark. "
    "Decide whether the model answer matches the gold answer in meaning."
)


def _answer_user(question: str, memories: str, question_date: str) -> str:
    today = (
        f"Today's date is {question_date}. Compute relative time from it.\n"
        if question_date
        else ""
    )
    return (
        f"{today}"
        f"Memories:\n{memories}\n\n"
        f"Question: {question}\n"
        "Answer concisely using only the memories above. "
        "If the memories do not contain the answer, reply: I don't know."
    )


# 拒答判定（LongMemEval judge 通行规则）：参考答案是拒答时，模型唯有也拒答才算对；
# 参考答案是具体事实时，模型拒答即错。
_ABSTENTION_RULE = (
    "If the gold answer indicates the information is unavailable/unanswerable, the response is "
    "correct only if it also clearly declines/abstains. If the gold answer is a concrete fact, "
    "an abstention is wrong."
)


def _judge_user(question: str, gold: str, answer: str, strict: bool) -> str:
    leniency = (
        "Require the model answer to be essentially equivalent to the gold answer."
        if strict
        else "Be lenient about phrasing, formatting, and extra detail; "
        "judge correctness of the core fact."
    )
    return (
        f"Question: {question}\n"
        f"Gold answer: {gold}\n"
        f"Model answer: {answer}\n\n"
        f"{leniency}\n{_ABSTENTION_RULE}\n"
        'Respond with JSON only: {"label": "CORRECT" or "WRONG", "reasoning": "..."}'
    )


def _parse_correct(content: str) -> Optional[bool]:
    """从 judge 输出里抽取 label（容忍前后缀文字，取首个 JSON 对象）。"""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            json_end = end + 1
            label = str(json.loads(text[start:json_end]).get("label", "")).strip().upper()
        except json.JSONDecodeError:
            label = ""
        if label == "CORRECT":
            return True
        if label == "WRONG":
            return False
    return None


def _parse_longmemeval_correct(content: str) -> Optional[bool]:
    """Parse the Mem0/LongMemEval judge's final yes/no verdict."""
    text = (content or "").strip().lower()
    marker = "</judge_thinking>"
    if marker in text:
        text = text.rsplit(marker, 1)[1].strip()
    verdict = text.strip("`*_ .!?\t\n\r")
    if verdict == "yes":
        return True
    if verdict == "no":
        return False
    yes_match = re.search(r"\byes\b", text)
    no_match = re.search(r"\bno\b", text)
    if yes_match and not no_match:
        return True
    if no_match and not yes_match:
        return False
    return None


class LLMJudge:
    """两步 LLM judge：召回记忆 → 合成答案 → 比对参考答案 → 1.0/0.0。"""

    def __init__(
        self,
        chat: ChatFn,
        strict: bool = False,
        prompt_profile: str = GENERIC_PROMPT_PROFILE,
        judge_chat: ChatFn | None = None,
    ) -> None:
        if prompt_profile not in _PROMPT_PROFILES:
            raise ValueError(f"unknown prompt profile: {prompt_profile}")
        self._answer_chat = chat
        self._judge_chat = judge_chat or chat
        self._strict = strict
        self._prompt_profile = prompt_profile
        self._records: list[dict[str, object]] = []

    @property
    def records(self) -> list[dict[str, object]]:
        return list(self._records)

    def __call__(self, query: str, expected: str, contexts, meta=None) -> float:
        metadata = meta or {}
        primary_cutoff = int(metadata.get("answer_cutoff", len(contexts)))
        return self.score_cutoffs(query, expected, contexts, metadata)[primary_cutoff]

    def score_cutoffs(self, query: str, expected: str, contexts, meta=None) -> dict[int, float]:
        """Generate and judge every requested cutoff while keeping one record per query."""
        metadata = meta or {}
        primary_cutoff = int(metadata.get("answer_cutoff", len(contexts)))
        raw_cutoffs = str(metadata.get("answer_cutoffs", primary_cutoff))
        cutoffs = list(
            dict.fromkeys(
                int(value.strip())
                for value in raw_cutoffs.split(",")
                if value.strip()
            )
        )
        if primary_cutoff not in cutoffs:
            cutoffs.insert(0, primary_cutoff)

        cutoff_records: dict[str, dict[str, object]] = {}
        scores: dict[int, float] = {}
        for cutoff in cutoffs:
            score, record = self._score_one(
                query,
                expected,
                contexts,
                metadata,
                cutoff,
            )
            scores[cutoff] = score
            cutoff_records[str(cutoff)] = record

        primary_record = dict(cutoff_records[str(primary_cutoff)])
        primary_record["cutoffs"] = cutoff_records
        self._records.append(primary_record)
        return scores

    def _score_one(
        self,
        query: str,
        expected: str,
        contexts,
        metadata,
        answer_cutoff: int,
    ) -> tuple[float, dict[str, object]]:
        question_date = metadata.get("question_date", "")
        answer_contexts = contexts[:answer_cutoff]
        prompt_context_dates = []
        prompt_message_dates = []
        prompt_event_dates = []
        prompt_retrieval_ranks = []
        if self._prompt_profile == LONGMEMEVAL_PROMPT_PROFILE:
            context_dates = metadata.get("context_dates", [])
            message_dates = metadata.get("context_message_dates", [])
            event_dates = metadata.get("context_event_dates", [])
            # Freeze Top-K membership and order exactly as returned by RRF.
            # Temporal fields annotate each unit but must not replace relevance
            # order after retrieval.
            answer_search_results = [
                {
                    "memory": content,
                    "created_at": (
                        context_dates[idx] if idx < len(context_dates) else ""
                    ),
                    "message_at": (
                        message_dates[idx] if idx < len(message_dates) else ""
                    ),
                    "event_at": event_dates[idx] if idx < len(event_dates) else "",
                    "retrieval_rank": idx + 1,
                }
                for idx, content in enumerate(answer_contexts)
            ]
            answer_contexts = [item["memory"] for item in answer_search_results]
            prompt_context_dates = [
                item.get("created_at", "") for item in answer_search_results
            ]
            prompt_message_dates = [
                item.get("message_at", "") for item in answer_search_results
            ]
            prompt_event_dates = [
                item.get("event_at", "") for item in answer_search_results
            ]
            prompt_retrieval_ranks = [
                item.get("retrieval_rank", 0) for item in answer_search_results
            ]
            answer_prompt = get_longmemeval_answer_prompt(
                question=query,
                search_results=answer_search_results,
                question_date=question_date,
            )
            answer = self._answer_chat("", answer_prompt).strip()
            judge_prompt = get_longmemeval_judge_prompt(
                question_type=metadata.get("question_type", ""),
                question_id=metadata.get("query_id", ""),
                question=query,
                answer=expected,
                response=answer,
                question_date=question_date,
            )
            verdict = self._judge_chat("", judge_prompt)
            parsed = _parse_longmemeval_correct(verdict)
        else:
            memories = (
                "\n".join(f"- {content}" for content in answer_contexts)
                or "(no memories retrieved)"
            )
            answer = self._answer_chat(
                _ANSWER_SYS,
                _answer_user(query, memories, question_date),
            ).strip()
            verdict = self._judge_chat(
                _JUDGE_SYS,
                _judge_user(query, expected, answer, self._strict),
            )
            parsed = _parse_correct(verdict)
        score = 1.0 if parsed else 0.0
        record = {
            "question": query,
            "query_id": metadata.get("query_id", ""),
            "gold_answer": expected,
            "model_answer": answer,
            "judge_output": verdict,
            "judge_parsed": parsed,
            "score": score,
            "prompt_profile": self._prompt_profile,
            "retrieved_context_count": len(contexts),
            "answer_cutoff": answer_cutoff,
            "context_count": len(answer_contexts),
            "context_chars": sum(len(content) for content in answer_contexts),
            "context_previews": [content[:2000] for content in answer_contexts],
            "context_contents": list(answer_contexts),
            "context_dates": prompt_context_dates,
            "context_message_dates": prompt_message_dates,
            "context_event_dates": prompt_event_dates,
            "context_retrieval_ranks": prompt_retrieval_ranks,
            "context_order": "rrf_retrieval_order_after_cutoff",
        }
        return score, record


def openai_chat(
    base_url: str,
    model: str,
    api_key: str,
    temperature: Optional[float] = None,
    max_tokens: int | None = None,
    timeout: float = 60.0,
    audit_category: str = "",
) -> ChatFn:
    """构造 OpenAI 兼容的 ``chat`` 实现（懒加载 ``openai``，未安装时给出明确提示）。"""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ImportError("需要 `pip install openai` 才能用 openai_chat") from exc

    client = OpenAI(base_url=base_url, api_key=api_key)
    audit_path_raw = os.getenv("LME_CLIENT_API_AUDIT_PATH", "").strip()
    audit_path = Path(audit_path_raw) if audit_path_raw else None
    audit_lock = threading.Lock()

    def _append_audit(row: dict[str, object]) -> None:
        if audit_path is None:
            return
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_lock, audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _chat(system: str, user: str) -> str:
        messages = [{"role": "user", "content": user}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        kwargs = {
            "model": model,
            "messages": messages,
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        started = time.perf_counter()
        category = audit_category or (
            "judge" if "you are an answer judge" in user.lower() else "answer"
        )
        try:
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            usage = resp.usage.model_dump() if resp.usage is not None else {}
            _append_audit(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sample": os.getenv("LME_AUDIT_SAMPLE", ""),
                    "category": category,
                    "status": "success",
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "system_chars": len(system),
                    "user_chars": len(user),
                    "system_sha256": _sha256(system),
                    "user_sha256": _sha256(user),
                    "response_chars": len(content),
                    "response_sha256": _sha256(content),
                    "empty_response": not bool(content.strip()),
                    "finish_reason": resp.choices[0].finish_reason,
                    "usage": usage,
                }
            )
            return content
        except Exception as exc:
            _append_audit(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sample": os.getenv("LME_AUDIT_SAMPLE", ""),
                    "category": category,
                    "status": "error",
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "system_chars": len(system),
                    "user_chars": len(user),
                    "system_sha256": _sha256(system),
                    "user_sha256": _sha256(user),
                    "error_type": type(exc).__name__,
                }
            )
            raise

    return _chat
