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

from typing import Any, Dict, List, Sequence

from jiuwen_memory.retrieval.types import RecallChannel, ScoredUnit
from jiuwen_memory.storage.types import ScoredID


def aggregate_to_units(
    hits: Sequence[ScoredID],
    records: Sequence[Any],  # VectorRecord / Document：任意带 .id 与 .metadata 的记录
    channel: RecallChannel,
) -> List[ScoredUnit]:
    """把命中按 ``metadata['unit_id']`` 归并到 unit 粒度，同 unit 取 MaxP，按分降序。"""
    meta_by_id: Dict[str, dict] = {r.id: getattr(r, "metadata", {}) or {} for r in records}
    best: Dict[str, float] = {}
    for hit in hits:
        uid = meta_by_id.get(hit.id, {}).get("unit_id") or hit.id
        if hit.score > best.get(uid, float("-inf")):
            best[uid] = hit.score
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [ScoredUnit(unit_id=uid, score=score, channel=channel) for uid, score in ranked]
