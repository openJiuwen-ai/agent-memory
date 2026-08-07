"""分层召回的通道内归并——各 Fuser 融合前的统一前处理。

L0/L1 分层召回下，同一通道存在多个 recall 实例（L2 content / L0 概要 / L1 片段），
而 ``KeywordRecaller`` / ``VectorRecaller`` 的 ``channel()`` 对三层返回同一个值——
``layer`` 是 recaller 上的独立属性，融合层看不见。因此 ``Fuser.fuse`` 收到的
``candidates`` 里，同一通道会占据多个列表，若直接按"一个列表 = 一路信号"处理：

- 计分类融合（RRF/加法）会把同 unit 的多层命中重复累加，分数偏向持有 layers 的 unit；
- 归一化类融合会按层各自取基准——l0 若只召回 2 条（最高分 3），其弱候选会被归一化
  到 1.0，与 l2 最强候选（最高分 20）同级。

两者都是**索引覆盖差异，不是相关性差异**：unit 是否具备 layers 取决于 LayerAnnotator
是否为其生成分层（短 content 不生成），与它跟查询的相关程度无关。

正确语义是：分层是同一通道的多个**索引入口**，不是独立的信号源。故融合前先按通道
归并、同 unit 取最高分（MaxP），与向量召回内部 chunk→unit 的归并口径一致。

未启用分层时每通道只有一个列表，归并为恒等变换，融合行为不变。
"""

from __future__ import annotations

from common.type_def import ScoredCandidate
from retrieval.types import RecallChannel


def merge_layered_channels(
    candidates: list[list[ScoredCandidate]],
) -> list[list[ScoredCandidate]]:
    """把同一通道的多路分层召回结果归并为一路（同 unit 取最高分）。

    :param candidates: 各 recall 实例的候选列表；分层下同一通道占多个列表。
    :returns: 每个在场通道一个列表，按分数降序；通道顺序按其首次出现顺序保留。

    空列表不产生通道（与融合侧"仅在场通道参与计分"的口径一致）。
    """
    buckets: dict[RecallChannel, dict[str, ScoredCandidate]] = {}
    for one_channel in candidates:
        if not one_channel:
            continue
        bucket = buckets.setdefault(one_channel[0].channel, {})
        for su in one_channel:
            current = bucket.get(su.unit_id)
            if current is None or su.score > current.score:
                bucket[su.unit_id] = su
    return [
        sorted(bucket.values(), key=lambda su: su.score, reverse=True)
        for bucket in buckets.values()
    ]
