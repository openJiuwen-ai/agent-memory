# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""召回命中 → unit 粒度归并（chunk→unit 的 MaxP 聚合）。

各通道索引的记录 id 粒度不一：向量索引按 chunk 建（id 为 chunk 复合 id），全文索引
按 unit 建（id 即 unit.id）。但构建层在每条记录的 ``metadata['unit_id']`` 都写了所属
unit 的 id（见 construction 的 IndexBuilder）。本模块据此把命中**统一到 unit 粒度**：
同一 unit 的多个 chunk 命中折叠为一条，取最高分（MaxP）。

这样向量通道回传的就是 ``unit.id``（而非 chunk 复合 id）——UnitReader 才能点读真源、
Fuser 才能跨通道合并同一 unit。``metadata`` 缺 ``unit_id`` 时回退到记录 id（兼容直接
按 unit.id 建索引、未写 metadata 的简化场景）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

from jiuwen_memory.common.graphmem_semantics import FEATURES_KEY
from jiuwen_memory.common.type_def import RecallChannel, ScoredUnit
from jiuwen_memory.common.type_def.retrieval import ContentMatch
from jiuwen_memory.storage.types import ScoredID


def _content_match(hit, metadata, channel):
    start, end, seq = (metadata.get(key) for key in ("content_start", "content_end", "seq"))
    digest = metadata.get("indexed_text_sha256")
    if (channel is not RecallChannel.VECTOR or metadata.get("content_layer") != "l2"
            or any(type(value) is not int for value in (start, end, seq))
            or not 0 <= start < end or seq < 0
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(hit.id, str) or len(hit.id) > 256
            or type(hit.score) not in (int, float)
            or not math.isfinite(hit.score) or hit.score <= 0):
        return None
    return ContentMatch(hit.id, seq, start, end, digest, hit.score)


def aggregate_to_units(
    hits: Sequence[ScoredID],
    records: Sequence[Any],  # VectorRecord / Document：任意带 .id 与 .metadata 的记录
    channel: RecallChannel,
    *,
    recall_entry_semantics: bool = False,
) -> list[ScoredUnit]:
    """把命中按 ``metadata['unit_id']`` 归并到 unit 粒度，同 unit 取 MaxP，按分降序。"""
    by_id = {record.id: record for record in records}
    best: dict[str, float] = {}
    matches: dict[str, ContentMatch | None] = {}
    indexed_semantics: dict[str, str] = {}
    keyword_text: dict[str, str] = {}
    for hit in hits:
        record = by_id.get(hit.id)
        metadata = getattr(record, "metadata", {}) or {}
        uid = metadata.get("unit_id") or hit.id
        if hit.score > best.get(uid, float("-inf")):
            best[uid] = hit.score
            matches[uid] = _content_match(hit, metadata, channel)
            if recall_entry_semantics:
                encoded = metadata.get(f"system_metadata.{FEATURES_KEY}")
                if (metadata.get("hierarchy_role") == "time_span"
                        and isinstance(encoded, str) and 0 < len(encoded) <= 131072):
                    indexed_semantics[uid] = encoded
                else:
                    indexed_semantics.pop(uid, None)
                if (channel is RecallChannel.KEYWORD
                        and metadata.get("hierarchy_role") in {"time_span", "snapshot"}):
                    text = getattr(record, "text", None)
                    if isinstance(text, str) and text:
                        keyword_text[uid] = text[:8192]
                    else:
                        keyword_text.pop(uid, None)
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [ScoredUnit(unit_id=uid, score=score, channel=channel,
                       content_match=matches[uid], entry_semantics=indexed_semantics.get(uid),
                       entry_keyword_text=keyword_text.get(uid, ""))
            for uid, score in ranked]
