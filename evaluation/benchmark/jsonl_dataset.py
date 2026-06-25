"""通用 JSONL 评测标注数据集——自定义评测标注与回归数据的标准载体。

每行一条 JSON 记录，``type`` 区分两类：

    {"type": "seed",  "key": "m1", "content": "...", "tags": ["coffee"],
     "scope": {"org": "eval", "user": "u1"}}
    {"type": "query", "query_id": "q1", "text": "...", "relevant_keys": ["m1"],
     "top_k": 5, "expected_answer": "", "scope": {"org": "eval", "user": "u1"}}

``scope`` 可省略（用 ``default_scope``）。``relevant_keys`` 指向 seed 的 ``key``。
空行与 ``#`` 起始的注释行忽略。
"""

from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence

from common.type_def import Scope
from evaluation.core.types import Dataset, MemorySeed, QueryCase

_DEFAULT_SCOPE = Scope(org="eval", user="u1")


def _to_scope(raw: Optional[dict], fallback: Scope) -> Scope:
    if not raw:
        return fallback
    return Scope(
        org=raw.get("org", fallback.org),
        user=raw.get("user", fallback.user),
        agent=raw.get("agent", getattr(fallback, "agent", "")),
        session=raw.get("session", getattr(fallback, "session", "")),
    )


class JsonlDataset(Dataset):
    """从 ``.jsonl`` 评测标注文件加载 seeds + queries。"""

    def __init__(
        self,
        path: str,
        name: Optional[str] = None,
        default_scope: Scope = _DEFAULT_SCOPE,
    ) -> None:
        self.name = name or os.path.splitext(os.path.basename(path))[0]
        self._seeds: List[MemorySeed] = []
        self._queries: List[QueryCase] = []
        self._load(path, default_scope)

    def _load(self, path: str, default_scope: Scope) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                record = json.loads(line)
                kind = record.get("type")
                scope = _to_scope(record.get("scope"), default_scope)
                if kind == "seed":
                    self._seeds.append(
                        MemorySeed(
                            key=record["key"],
                            content=record["content"],
                            scope=scope,
                            tags=list(record.get("tags", [])),
                            metadata=dict(record.get("metadata", {})),
                        )
                    )
                elif kind == "query":
                    self._queries.append(
                        QueryCase(
                            query_id=record["query_id"],
                            text=record["text"],
                            scope=scope,
                            relevant_keys=set(record.get("relevant_keys", [])),
                            expected_answer=record.get("expected_answer", ""),
                            top_k=int(record.get("top_k", 10)),
                        )
                    )
                else:
                    raise ValueError(f"unknown record type: {kind!r} in {path}")

    def seeds(self) -> Sequence[MemorySeed]:
        return self._seeds

    def queries(self) -> Sequence[QueryCase]:
        return self._queries
