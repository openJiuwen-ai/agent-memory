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
- **seeds**：每条 turn 一条，``key = "{question_id}/{session_id}#{turn}"``，
  content = ``[role]: content``，``occurred_at`` 取会话时间 + 行内秒偏移。
- **queries**：每个 sample 一道，``expected_answer`` = answer，
  ``relevant_keys`` = 证据 turn 的 key（优先 turn 级 ``has_answer``，否则回退到
  ``answer_session_ids`` 整会话）。``metadata`` 记 ``question_type``（=``bucket``，
  供按类型分桶）、``question_date``（judge 用作「今天」）、``abstention``。
- **隔离**：每个 sample 用独立 scope ``Scope(org=..., user=question_id)``——每道题
  只在自己的 haystack 内召回（per-sample 隔离，对应架构的 scope 原生隔离）。

数据集需下载到 ``evaluation/benchmark/data/longmemeval_s.json``
（https://github.com/xiaowu0162/LongMemEval ）；打分走端到端 QA（注入 LLM judge）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from jiuwen_memory.common.type_def import Scope
from evaluation.core.types import Dataset, MemorySeed, QueryCase

_DEFAULT_DATA = "evaluation/benchmark/data/longmemeval_s.json"
_DATE_FMT = "%Y/%m/%d (%a) %H:%M"  # 如 "2023/05/20 (Sat) 10:30"


def _parse_dt(date_str: str) -> Optional[datetime]:
    """解析输入数据并返回结构化结果。

    Args:
        date_str: 参数 date_str（str）。

    Returns:
        返回 Optional[datetime]。
    """
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
        samples: Optional[Sequence[int]] = None,
        max_questions: Optional[int] = None,
        scope_org: str = "longmemeval",
    ) -> None:
        """初始化 LongMemEvalDataset。

        Args:
            path: 参数 path（str）。
            samples: 参数 samples（Optional[Sequence[int]]）。
            max_questions: 参数 max_questions（Optional[int]）。
            scope_org: 参数 scope_org（str）。
        """
        self._path = path
        self._scope_org = scope_org
        self._seeds: List[MemorySeed] = []
        self._queries: List[QueryCase] = []
        if os.path.exists(path):
            self._parse(path, samples, max_questions)

    def seeds(self) -> Sequence[MemorySeed]:
        """执行 `seeds` 操作。

        Returns:
            返回 Sequence[MemorySeed]。
        """
        self._require_loaded()
        return self._seeds

    def queries(self) -> Sequence[QueryCase]:
        """执行 `queries` 操作。

        Returns:
            返回 Sequence[QueryCase]。
        """
        self._require_loaded()
        return self._queries

    # -- 解析 --------------------------------------------------------------- #
    def _parse(
        self,
        path: str,
        samples: Optional[Sequence[int]],
        max_questions: Optional[int],
    ) -> None:
        """解析输入数据并返回结构化结果。

        Args:
            path: 参数 path（str）。
            samples: 参数 samples（Optional[Sequence[int]]）。
            max_questions: 参数 max_questions（Optional[int]）。
        """
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        indices = list(samples) if samples is not None else range(len(data))
        for idx in indices:
            self._parse_sample(data[idx])

        if max_questions is not None:
            self._queries = self._queries[:max_questions]

    def _parse_sample(self, sample: dict) -> None:
        """解析输入数据并返回结构化结果。

        Args:
            sample: 参数 sample（dict）。
        """
        qid = sample["question_id"]
        scope = Scope(org=self._scope_org, user=qid)
        sessions = sample.get("haystack_sessions", [])
        session_ids = sample.get("haystack_session_ids", [])
        dates = sample.get("haystack_dates", [])
        answer_session_ids = set(sample.get("answer_session_ids", []))

        evidence_keys: set[str] = set()
        session_keys: dict[str, list[str]] = {}  # session_id -> 其全部 turn key
        for s_idx, turns in enumerate(sessions):
            sid = session_ids[s_idx] if s_idx < len(session_ids) else f"session_{s_idx + 1}"
            base_dt = _parse_dt(dates[s_idx]) if s_idx < len(dates) else None
            session_keys[sid] = []
            for t_idx, turn in enumerate(turns):
                key = f"{qid}/{sid}#{t_idx}"
                session_keys[sid].append(key)
                self._seeds.append(
                    MemorySeed(
                        key=key,
                        content=f"[{turn.get('role', 'user')}]: {turn.get('content', '')}",
                        scope=scope,
                        tags=[sid],
                        metadata={"session": sid, "role": turn.get("role", "")},
                        occurred_at=(base_dt + timedelta(seconds=t_idx) if base_dt else None),
                    )
                )
                if turn.get("has_answer"):  # turn 级证据标注（更精确）
                    evidence_keys.add(key)

        # 无 turn 级 has_answer 时，回退到会话级证据标注（answer_session_ids 整会话）。
        if not evidence_keys:
            for sid in answer_session_ids:
                evidence_keys.update(session_keys.get(sid, []))

        qtype = sample.get("question_type", "")
        self._queries.append(
            QueryCase(
                query_id=qid,
                text=sample["question"],
                scope=scope,
                relevant_keys=evidence_keys,
                expected_answer=str(sample.get("answer", "")),
                top_k=20,
                metadata={
                    "question_type": qtype,
                    "bucket": qtype,
                    "question_date": sample.get("question_date", ""),
                    "abstention": "true" if str(qid).endswith("_abs") else "false",
                },
            )
        )

    # -- Dataset 接口 ------------------------------------------------------- #
    def _require_loaded(self) -> None:
        """校验并取得必需的资源或参数。

        Raises:
            FileNotFoundError: 执行失败时抛出。
        """
        if not self._seeds and not self._queries:
            raise FileNotFoundError(
                f"LongMemEval 数据缺失：{self._path}。从 "
                "https://github.com/xiaowu0162/LongMemEval 下载（如 longmemeval_s.json）后重试。"
            )
