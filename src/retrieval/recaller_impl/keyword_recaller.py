"""最小实现：:class:`~retrieval.recaller.Recaller` 的 KEYWORD 通道。

消费 ``ParsedQuery``，组装 :class:`~storage.types.TextQuery`（scope 落查询的专用
scope 入参做原生隔离、``scalar_filters`` 落 filters 做硬过滤），经注入的
:class:`~storage.fulltext.FulltextStore` 召回，再按记录 ``metadata['unit_id']`` 归并到
unit 粒度（与向量通道同一套口径，全文按 unit 建索引时为恒等映射）。多路并行与合并由
Retriever/Fuser 负责，本通道不感知其他通道。
"""

from __future__ import annotations

from typing import List

from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from storage.fulltext import FulltextProducer, FulltextStore
from storage.types import TextQuery

from .unit_aggregation import aggregate_to_units


class KeywordRecaller(Recaller):
    """关键词/全文召回路（包一个 FulltextStore）。"""

    def __init__(self, fulltext: FulltextStore) -> None:
        self._fulltext = fulltext

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.KEYWORD

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        tq = TextQuery(
            text=query.rewritten or query.raw,
            top_k=top_k,
            filters=query.scalar_filters,  # scope 不混进 filters，单独走入参
        )
        hits = self._fulltext.search(scope, tq)
        records = self._fulltext.get(scope, [h.id for h in hits])
        return aggregate_to_units(hits, records, RecallChannel.KEYWORD)


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@RecallerProducer.register("keyword")
def _build(config):
    return KeywordRecaller(FulltextProducer.dep(config, default="memory"))
