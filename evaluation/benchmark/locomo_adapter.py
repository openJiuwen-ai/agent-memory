"""LoCoMo 适配器——把 ``locomo10.json`` 解析为 (seeds, queries)（端到端 QA）。

LoCoMo（Evaluating Very Long-Term Conversational Memory, ACL 2024）schema：

    [ {  # 一个 sample = 一段超长多会话对话
        "conversation": {
            "speaker_a": "...", "speaker_b": "...",
            "session_1": [ {"speaker", "text", "img_url", "blip_caption"}, ... ],
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_2": [...], ...
        },
        "qa": [ {"question", "answer", "category"(1-5), "evidence": ["D1:3", ...]}, ... ]
      }, ... ]

映射（按 LoCoMo 官方 schema 解析）：
- **seeds**：每条消息一条，``key = "{sample_id}/D{session}:{turn}"``（turn 为 1-based，
  与 evidence 同构），content = ``[speaker]: text``（图片用 blip_caption 文本兜底），
  ``occurred_at`` 取 session 时间 + 行内秒偏移以保序。
- **queries**：每道 QA 一条，``expected_answer`` = 标注答案，``relevant_keys`` = 由
  ``evidence`` 的 ``D{session}:{turn}`` 映射到对应 seed key，``metadata.category`` 记类目。
  **category 5（对抗题）跳过**（与 LoCoMo judge/stat 约定一致）。
- **隔离**：每个 sample 用独立 scope ``Scope(org=..., user=sample_id)``——不同对话互不串味，
  对应架构的 scope 原生隔离（recall 在该 sample 范围内召回）。

数据集需自行下载到 ``evaluation/benchmark/data/locomo10.json``
（https://github.com/snap-research/LoCoMo ）；打分走端到端 QA，需注入 LLM judge
（见 :mod:`evaluation.metrics.llm_judge`）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from common.type_def import Scope

from evaluation.core.types import Dataset, MemorySeed, QueryCase

_DEFAULT_DATA = "evaluation/benchmark/data/locomo10.json"
_DATE_FMT = "%I:%M %p on %d %B, %Y"  # 如 "1:56 pm on 8 May, 2023"
_ADVERSARIAL = 5  # category 5：对抗题，judge/stat 一律跳过


def _parse_dt(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str.strip(), _DATE_FMT)
    except (ValueError, AttributeError):
        return None


def _session_num(key: str) -> int:
    return int(key.split("_")[1])


def _message_content(msg: dict) -> str:
    text = f"[{msg.get('speaker', 'unknown')}]: {msg.get('text', '')}"
    caption = str(msg.get("blip_caption", "") or "")
    img = msg.get("img_url")
    if img and caption:
        text += f" [Image: {caption}]"
    return text


class LoCoMoDataset(Dataset):
    """LoCoMo → (seeds, queries) 适配器。"""

    name = "locomo"

    def __init__(
        self,
        path: str = _DEFAULT_DATA,
        samples: Optional[Sequence[int]] = None,
        max_questions: Optional[int] = None,
        scope_org: str = "locomo",
    ) -> None:
        self._path = path
        self._scope_org = scope_org
        self._seeds: List[MemorySeed] = []
        self._queries: List[QueryCase] = []
        if os.path.exists(path):
            self._parse(path, samples, max_questions)

    # -- 解析 --------------------------------------------------------------- #
    def _parse(
        self,
        path: str,
        samples: Optional[Sequence[int]],
        max_questions: Optional[int],
    ) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        indices = list(samples) if samples is not None else range(len(data))
        for idx in indices:
            sample_id = f"sample_{idx}"
            scope = Scope(org=self._scope_org, user=sample_id)
            self._parse_sample(data[idx], sample_id, scope)

        if max_questions is not None:
            self._queries = self._queries[:max_questions]

    def _parse_sample(self, sample: dict, sample_id: str, scope: Scope) -> None:
        conv = sample.get("conversation", {})
        session_keys = sorted(
            (k for k in conv if k.startswith("session_") and not k.endswith("_date_time")),
            key=_session_num,
        )
        for sk in session_keys:
            base_dt = _parse_dt(conv.get(f"{sk}_date_time", ""))
            for turn, msg in enumerate(conv[sk], start=1):  # turn 1-based，对齐 evidence
                key = f"{sample_id}/D{_session_num(sk)}:{turn}"
                self._seeds.append(
                    MemorySeed(
                        key=key,
                        content=_message_content(msg),
                        scope=scope,
                        tags=[sk],
                        metadata={"session": sk, "speaker": msg.get("speaker", "")},
                        occurred_at=(base_dt + timedelta(seconds=turn) if base_dt else None),
                    )
                )

        for q_idx, qa in enumerate(sample.get("qa", [])):
            category = qa.get("category", "")
            if category == _ADVERSARIAL:
                continue
            relevant = {
                f"{sample_id}/{ev.strip()}" for ev in qa.get("evidence", []) if ev
            }
            self._queries.append(
                QueryCase(
                    query_id=f"{sample_id}_qa{q_idx}",
                    text=qa["question"],
                    scope=scope,
                    relevant_keys=relevant,
                    expected_answer=str(qa.get("answer", "")),
                    top_k=20,
                    metadata={"category": str(category), "bucket": f"cat{category}"},
                )
            )

    # -- Dataset 接口 ------------------------------------------------------- #
    def _require_loaded(self) -> None:
        if not self._seeds and not self._queries:
            raise FileNotFoundError(
                f"LoCoMo 数据缺失：{self._path}。从 "
                "https://github.com/snap-research/LoCoMo 下载 locomo10.json 后重试。"
            )

    def seeds(self) -> Sequence[MemorySeed]:
        self._require_loaded()
        return self._seeds

    def queries(self) -> Sequence[QueryCase]:
        self._require_loaded()
        return self._queries
