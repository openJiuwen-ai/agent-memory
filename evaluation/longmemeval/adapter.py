"""LongMemEval 适配器——把 ``longmemeval_*.json`` 解析为 (seeds, queries)。

LongMemEval（Benchmarking Chat Assistants on Long-Term Interactive Memory, ICLR 2025）
schema（按 LongMemEval 官方字段解析）：

    [ {  # 一个 sample = 一道题 + 它自己的 haystack（干扰会话海）
        "question_id": "...",            # 唯一 id；以 "_abs" 结尾表示拒答题
        "question_type": "single-session-user | temporal-reasoning | knowledge-update | ...",
        "question": "...", "answer": "...",
        "question_date": "2023/05/20 (Sat) 10:30",   # 提问时刻（=「今天」，供时序题）
        "haystack_session_ids": ["s1", ...],
        "haystack_dates": ["2023/05/01 (Mon) 09:00", ...],
        "haystack_sessions": [ [ {"role", "content", "has_answer"?}, ... ], ... ],
        "answer_session_ids": ["s3", ...]            # 含证据的会话 id（会话级标注）
      }, ... ]

映射：
- **seeds**：支持 ``turn`` / ``dialogue_turn`` / ``session`` 三种粒度。
  ``turn`` 按单条消息写入；``dialogue_turn`` 将相邻的 user + assistant
  合并为一个对话轮次，不规则或孤立消息保持单条；``session``
  按整个会话写入。
  正文沿用 LoCoMo 的 ``[role]: content`` 口径；会话时间通过
  ``observation_date`` 与 ``occurred_at`` 传给内核。
- **上下文口径**：默认写入完整 haystack；显式设置 ``oracle_sessions=True`` 时，
  只写入 ``answer_session_ids`` 指定的会话，以复现 Oracle-context 协议。
- **queries**：每个 sample 一道，``expected_answer`` = answer，
  ``relevant_keys`` = 证据 turn 的 key（优先 turn 级 ``has_answer``，否则回退到
  ``answer_session_ids`` 整会话）。``metadata`` 记 ``question_type``（=``bucket``，
  供按类型分桶）、``question_date``（judge 用作「今天」）、``abstention``。
- **隔离**：每个 sample 用独立 scope ``Scope(org=..., user=question_id)``——每道题
  只在自己的 haystack 内召回（per-sample 隔离，对应架构的 scope 原生隔离）。

数据集需下载到 ``evaluation/benchmark/data/longmemeval_s_cleaned.json``
（https://github.com/xiaowu0162/LongMemEval ）；打分走端到端 QA（注入 LLM judge）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from jiuwen_memory.common.type_def import Scope
from evaluation.longmemeval.types import Dataset, MemorySeed, QueryCase
from jiuwen_memory.retrieval.types import DisclosureLevel

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "datasets", "longmemeval")
_DEFAULT_DATA = os.path.join(_DATA_DIR, "longmemeval_oracle.json")
_LEGACY_DATA = os.path.join(_DATA_DIR, "longmemeval_s_cleaned.json")
_DATE_FMT = "%Y/%m/%d (%a) %H:%M"  # 如 "2023/05/20 (Sat) 10:30"
_GRANULARITIES = {"turn", "dialogue_turn", "session"}
LONG_TURN_CONTEXT_KEY = "longmemeval_sentence_context"
_SENTENCE_ENDINGS = frozenset("。！？!?；;\n\r")
_SENTENCE_CLOSERS = frozenset("\"'”’）)]}")
_SOFT_BREAKS = frozenset("，,、：: \t")


@dataclass(frozen=True)
class _IngestGroup:
    start: int
    end: int
    turns: tuple[dict[str, Any], ...]
    suffix: str
    granularity: str
    context: str = ""


def _parse_dt(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str.strip(), _DATE_FMT)
    except (ValueError, AttributeError):
        return None


class LongMemEvalDataset(Dataset):
    """LongMemEval → (seeds, queries) 适配器。"""

    name = "longmemeval"

    def __init__(
        self,
        path: str = _DEFAULT_DATA,
        samples: Sequence[int] | None = None,
        max_questions: int | None = None,
        scope_org: str = "longmemeval",
        granularity: Literal["turn", "dialogue_turn", "session"] = "turn",
        top_k: int = 200,
        answer_cutoff: int = 50,
        answer_cutoffs: Sequence[int] = (50, 200),
        infer: bool = False,
        oracle_sessions: bool = False,
        dialogue_turn_max_chars: int = 4096,
    ) -> None:
        if granularity not in _GRANULARITIES:
            raise ValueError(
                f"LongMemEval granularity must be one of {sorted(_GRANULARITIES)}, "
                f"got {granularity!r}"
            )
        if top_k <= 0:
            raise ValueError(f"LongMemEval top_k must be positive, got {top_k}")
        if answer_cutoff <= 0 or answer_cutoff > top_k:
            raise ValueError(
                "LongMemEval answer_cutoff must be positive and no greater than top_k, "
                f"got {answer_cutoff}/{top_k}"
            )
        normalized_cutoffs = tuple(dict.fromkeys(answer_cutoffs))
        if not normalized_cutoffs or any(
            cutoff <= 0 or cutoff > top_k for cutoff in normalized_cutoffs
        ):
            raise ValueError(
                "LongMemEval answer_cutoffs must be positive and no greater than top_k, "
                f"got {normalized_cutoffs}/{top_k}"
            )
        if answer_cutoff not in normalized_cutoffs:
            raise ValueError("LongMemEval answer_cutoff must be included in answer_cutoffs")
        if max_questions is not None and max_questions < 0:
            raise ValueError(
                f"LongMemEval max_questions must be non-negative, got {max_questions}"
            )
        if dialogue_turn_max_chars <= 0:
            raise ValueError(
                "LongMemEval dialogue_turn_max_chars must be positive, "
                f"got {dialogue_turn_max_chars}"
            )
        self._path = self._resolve_path(path)
        self._scope_org = scope_org
        self._granularity = granularity
        self._top_k = top_k
        self._answer_cutoff = answer_cutoff
        self._answer_cutoffs = normalized_cutoffs
        self._infer = infer
        self._oracle_sessions = oracle_sessions
        self._dialogue_turn_max_chars = dialogue_turn_max_chars
        self._seeds: list[MemorySeed] = []
        self._queries: list[QueryCase] = []
        if os.path.exists(self._path):
            self._parse(self._path, samples, max_questions)

    @staticmethod
    def _resolve_path(path: str) -> str:
        """Cleaned split is canonical; keep the old local filename as a compatibility fallback."""
        if path == _DEFAULT_DATA and not os.path.exists(path) and os.path.exists(_LEGACY_DATA):
            return _LEGACY_DATA
        return path

    # -- 解析 --------------------------------------------------------------- #
    def _parse(
        self,
        path: str,
        samples: Sequence[int] | None,
        max_questions: int | None,
    ) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError("LongMemEval root must be a JSON array")

        indices = list(samples) if samples is not None else list(range(len(data)))
        if max_questions is not None:
            indices = indices[:max_questions]
        for idx in indices:
            if idx < 0 or idx >= len(data):
                raise IndexError(f"LongMemEval sample index out of range: {idx}")
            self._parse_sample(data[idx])

    def _parse_sample(self, sample: dict) -> None:
        if not isinstance(sample, dict):
            raise ValueError("LongMemEval sample must be a JSON object")
        qid = str(sample["question_id"])
        scope = Scope(org=self._scope_org, user=qid)
        sessions = sample.get("haystack_sessions", [])
        session_ids = sample.get("haystack_session_ids", [])
        dates = sample.get("haystack_dates", [])
        if not isinstance(sessions, list):
            raise ValueError(f"LongMemEval {qid}: haystack_sessions must be a list")
        if len(session_ids) != len(sessions) or len(dates) != len(sessions):
            raise ValueError(
                f"LongMemEval {qid}: sessions/session_ids/dates length mismatch "
                f"({len(sessions)}/{len(session_ids)}/{len(dates)})"
            )
        answer_session_ids = {str(sid) for sid in sample.get("answer_session_ids", [])}
        selected_session_count = 0

        evidence_keys: set[str] = set()
        session_keys: dict[str, list[str]] = {}  # session_id -> 该 session 的可评测 key
        sentence_fallback_request_count = 0
        for s_idx, turns in enumerate(sessions):
            sid = str(session_ids[s_idx])
            if self._oracle_sessions and sid not in answer_session_ids:
                continue
            selected_session_count += 1
            date_text = str(dates[s_idx])
            base_dt = _parse_dt(date_text)
            session_keys[sid] = []
            if self._granularity == "session":
                key = f"{qid}/{sid}"
                session_keys[sid].append(key)
                content = "\n".join(self._turn_content(turn) for turn in turns)
                self._seeds.append(
                    MemorySeed(
                        key=key,
                        content=content,
                        scope=scope,
                        tags=[sid],
                        metadata=self._seed_metadata(sid, "session", date_text),
                        occurred_at=base_dt,
                    )
                )
                if any(bool(turn.get("has_answer")) for turn in turns):
                    evidence_keys.add(key)
                continue

            if self._granularity == "dialogue_turn":
                for group in self._ingest_groups(turns):
                    start_idx, end_idx = group.start, group.end
                    dialogue_turn = list(group.turns)
                    if group.granularity == "sentence":
                        sentence_fallback_request_count += 1
                    key = f"{qid}/{sid}#{group.suffix}"
                    session_keys[sid].append(key)
                    metadata = self._seed_metadata(
                        sid, group.granularity, date_text
                    )
                    metadata.update(
                        {
                            "turn_start": str(start_idx),
                            "turn_end": str(end_idx),
                            "turn_roles": ",".join(
                                str(turn.get("role", "")) for turn in dialogue_turn
                            ),
                        }
                    )
                    if group.context:
                        metadata[LONG_TURN_CONTEXT_KEY] = group.context
                    self._seeds.append(
                        MemorySeed(
                            key=key,
                            content="\n".join(
                                self._turn_content(turn) for turn in dialogue_turn
                            ),
                            scope=scope,
                            tags=[sid],
                            metadata=metadata,
                            occurred_at=(
                                base_dt + timedelta(seconds=start_idx) if base_dt else None
                            ),
                        )
                    )
                    if any(bool(turn.get("has_answer")) for turn in dialogue_turn):
                        evidence_keys.add(key)
                continue

            for t_idx, turn in enumerate(turns):
                key = f"{qid}/{sid}#{t_idx}"
                session_keys[sid].append(key)
                self._seeds.append(
                    MemorySeed(
                        key=key,
                        content=self._turn_content(turn),
                        scope=scope,
                        tags=[sid],
                        metadata=self._seed_metadata(
                            sid, str(turn.get("role", "")), date_text
                        ),
                        occurred_at=(base_dt + timedelta(seconds=t_idx) if base_dt else None),
                    )
                )
                if turn.get("has_answer"):  # turn 级证据标注（更精确）
                    evidence_keys.add(key)

        # 无 turn 级 has_answer 时，回退到会话级证据标注（answer_session_ids 整会话）。
        if not evidence_keys:
            for sid in answer_session_ids:
                evidence_keys.update(session_keys.get(sid, []))

        qtype = str(sample.get("question_type", ""))
        is_abstention = qid.endswith("_abs")
        # Official abstention cases can retain evidence labels from the source
        # question used to construct the negative.  Those labels are not
        # relevant to the rewritten unanswerable question and must not become
        # IR gold positives.
        if is_abstention:
            evidence_keys.clear()
        evidence_source_keys = (
            {
                sid: set(session_keys.get(sid, []))
                for sid in answer_session_ids
                if sid in session_keys
            }
            if not is_abstention
            else {}
        )
        self._queries.append(
            QueryCase(
                query_id=qid,
                text=str(sample["question"]),
                scope=scope,
                relevant_keys=evidence_keys,
                relevant_source_keys=evidence_source_keys,
                expected_answer=str(sample.get("answer", "")),
                top_k=self._top_k,
                disclosure=DisclosureLevel.L2,
                metadata={
                    "query_id": qid,
                    "question_type": qtype,
                    "bucket": qtype,
                    "question_date": str(sample.get("question_date", "")),
                    "abstention": "true" if is_abstention else "false",
                    "context_mode": (
                        "oracle_sessions" if self._oracle_sessions else "full_haystack"
                    ),
                    "input_session_count": str(selected_session_count),
                    "haystack_session_count": str(len(sessions)),
                    "answer_cutoff": str(self._answer_cutoff),
                    "answer_cutoffs": ",".join(str(cutoff) for cutoff in self._answer_cutoffs),
                    "dialogue_turn_max_chars": str(self._dialogue_turn_max_chars),
                    "sentence_fallback_request_count": str(
                        sentence_fallback_request_count
                    ),
                },
            )
        )

    @staticmethod
    def _turn_content(turn: dict) -> str:
        if not isinstance(turn, dict):
            raise ValueError("LongMemEval turn must be a JSON object")
        return f"[{turn.get('role', 'user')}]: {turn.get('content', '')}"

    @staticmethod
    def _dialogue_turns(turns: list[dict]) -> list[tuple[int, int, list[dict]]]:
        """将相邻 user + assistant 合并为一个轮次，孤立消息不丢弃。"""
        grouped: list[tuple[int, int, list[dict]]] = []
        index = 0
        while index < len(turns):
            current = turns[index]
            if not isinstance(current, dict):
                raise ValueError("LongMemEval turn must be a JSON object")
            if (
                str(current.get("role", "")) == "user"
                and index + 1 < len(turns)
                and isinstance(turns[index + 1], dict)
                and str(turns[index + 1].get("role", "")) == "assistant"
            ):
                grouped.append((index, index + 1, [current, turns[index + 1]]))
                index += 2
                continue
            grouped.append((index, index, [current]))
            index += 1
        return grouped

    def _ingest_groups(self, turns: list[dict[str, Any]]) -> list[_IngestGroup]:
        groups: list[_IngestGroup] = []
        for start, end, dialogue_turn in self._dialogue_turns(turns):
            rendered = "\n".join(self._turn_content(turn) for turn in dialogue_turn)
            if len(rendered) <= self._dialogue_turn_max_chars:
                groups.append(
                    _IngestGroup(
                        start=start,
                        end=end,
                        turns=tuple(dialogue_turn),
                        suffix=f"{start}-{end}",
                        granularity="dialogue_turn",
                    )
                )
                continue
            groups.extend(
                self._sentence_fallback_groups(
                    dialogue_turn,
                    start=start,
                    max_chars=self._dialogue_turn_max_chars,
                )
            )
        return groups

    @classmethod
    def _sentence_fallback_groups(
        cls,
        dialogue_turn: list[dict[str, Any]],
        *,
        start: int,
        max_chars: int,
    ) -> list[_IngestGroup]:
        groups: list[_IngestGroup] = []
        previous_lines: list[str] = []
        fallback_index = 0
        for relative_index, original in enumerate(dialogue_turn):
            turn_index = start + relative_index
            role = str(original.get("role", "user"))
            role_prefix = f"[{role}]: "
            if max_chars <= len(role_prefix):
                raise ValueError(
                    "dialogue_turn_max_chars must exceed the rendered role prefix"
                )
            content_limit = max_chars - len(role_prefix)
            for sentence in cls._bounded_sentences(
                str(original.get("content", "")), content_limit
            ):
                synthetic = dict(original)
                synthetic["content"] = sentence
                current_line = cls._turn_content(synthetic)
                context_budget = max(0, max_chars - len(current_line))
                context = cls._tail_context(previous_lines, context_budget)
                groups.append(
                    _IngestGroup(
                        start=turn_index,
                        end=turn_index,
                        turns=(synthetic,),
                        suffix=(
                            f"{start}-{start + len(dialogue_turn) - 1}-"
                            f"sentence-{fallback_index}"
                        ),
                        granularity="sentence",
                        context=context,
                    )
                )
                previous_lines.append(current_line)
                fallback_index += 1
        return groups

    @classmethod
    def _bounded_sentences(cls, text: str, max_chars: int) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []

        sentences: list[str] = []
        sentence_start = 0
        index = 0
        while index < len(text):
            char = text[index]
            after_closers = index + 1
            while (
                after_closers < len(text)
                and text[after_closers] in _SENTENCE_CLOSERS
            ):
                after_closers += 1
            is_period_end = char == "." and (
                after_closers == len(text) or text[after_closers].isspace()
            )
            if char in _SENTENCE_ENDINGS or is_period_end:
                sentence_end = index + 1
                while (
                    sentence_end < len(text)
                    and text[sentence_end] in _SENTENCE_CLOSERS
                ):
                    sentence_end += 1
                sentence = text[sentence_start:sentence_end].strip()
                if sentence:
                    sentences.extend(cls._split_oversized_sentence(sentence, max_chars))
                sentence_start = sentence_end
                index = sentence_end
                continue
            index += 1

        trailing = text[sentence_start:].strip()
        if trailing:
            sentences.extend(cls._split_oversized_sentence(trailing, max_chars))
        return sentences

    @staticmethod
    def _split_oversized_sentence(sentence: str, max_chars: int) -> list[str]:
        parts: list[str] = []
        remaining = sentence.strip()
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            lower_bound = max_chars // 2
            split_at = max(
                (
                    index + 1
                    for index, char in enumerate(window)
                    if char in _SOFT_BREAKS
                ),
                default=0,
            )
            if split_at < lower_bound:
                split_at = max_chars
            part = remaining[:split_at].strip()
            if part:
                parts.append(part)
            remaining = remaining[split_at:].strip()
        if remaining:
            parts.append(remaining)
        return parts

    @staticmethod
    def _tail_context(lines: list[str], max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        selected: list[str] = []
        used = 0
        for line in reversed(lines):
            extra = len(line) + (1 if selected else 0)
            if used + extra > max_chars:
                break
            selected.append(line)
            used += extra
        return "\n".join(reversed(selected))

    def _seed_metadata(self, sid: str, role: str, date_text: str) -> dict[str, str]:
        occurred_at = _parse_dt(date_text)
        metadata = {
            "session": sid,
            "role": role,
            "session_date": date_text,
            "observation_date": occurred_at.strftime("%Y-%m-%d") if occurred_at else "",
            "granularity": self._granularity,
            "retain_source": "false",
        }
        if self._infer:
            metadata["infer"] = "true"
        return metadata

    # -- Dataset 接口 ------------------------------------------------------- #
    def _require_loaded(self) -> None:
        if not self._seeds and not self._queries:
            raise FileNotFoundError(
                f"LongMemEval 数据缺失：{self._path}。从 "
                "https://github.com/xiaowu0162/LongMemEval 下载 "
                "longmemeval_s_cleaned.json 后重试。"
            )

    def seeds(self) -> Sequence[MemorySeed]:
        self._require_loaded()
        return self._seeds

    def queries(self) -> Sequence[QueryCase]:
        self._require_loaded()
        return self._queries
