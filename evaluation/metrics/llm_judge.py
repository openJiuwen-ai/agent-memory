"""可插拔 LLM judge——端到端 QA 的「答案合成 + 判分」实现。

端到端评测分两步 LLM 调用：① 从召回的记忆合成答案；② LLM-as-judge
比对参考答案给 CORRECT/WRONG。本模块把两步封装成一个符合
:data:`evaluation.metrics.qa_metrics.JudgeFn` 的可调用，注入即用：

    from evaluation.metrics.llm_judge import LLMJudge, openai_chat
    judge = LLMJudge(chat=openai_chat(base_url=..., model=..., api_key=...))
    runner = Runner([ir_metrics(), perf_metrics(), qa_accuracy(judge=judge)])

``chat`` 是 ``(system, user) -> str`` 的薄抽象——provider 无关：``openai_chat`` 给一个
OpenAI 兼容实现（智谱/豆包/OpenAI 等填各自 ``base_url`` 即可），也可注入任意自定义
实现（便于离线测试用确定性桩）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

# (system_prompt, user_prompt) -> 模型输出文本
ChatFn = Callable[[str, str], str]

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


class LLMJudge:
    """两步 LLM judge：召回记忆 → 合成答案 → 比对参考答案 → 1.0/0.0。"""

    def __init__(self, chat: ChatFn, strict: bool = False) -> None:
        self._chat = chat
        self._strict = strict

    def __call__(self, query: str, expected: str, contexts, meta=None) -> float:
        question_date = (meta or {}).get("question_date", "")
        memories = "\n".join(f"- {c}" for c in contexts) or "(no memories retrieved)"
        answer = self._chat(_ANSWER_SYS, _answer_user(query, memories, question_date)).strip()
        verdict = self._chat(_JUDGE_SYS, _judge_user(query, expected, answer, self._strict))
        return 1.0 if _parse_correct(verdict) else 0.0


def openai_chat(
    base_url: str,
    model: str,
    api_key: str,
    temperature: Optional[float] = None,
    timeout: float = 60.0,
) -> ChatFn:
    """构造 OpenAI 兼容的 ``chat`` 实现（懒加载 ``openai``，未安装时给出明确提示）。"""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ImportError("需要 `pip install openai` 才能用 openai_chat") from exc

    client = OpenAI(base_url=base_url, api_key=api_key)

    def _chat(system: str, user: str) -> str:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    return _chat
