"""最小实现：:class:`~retrieval.recaller.Recaller` 的 VECTOR 通道。

消费 ``ParsedQuery.vector``（由 QueryParser 经 Embedder 产出），组装
:class:`~storage.types.VectorQuery`（scope 走入参隔离、``scalar_filters`` 走硬过滤），
经注入的 :class:`~storage.vector.VectorStore` 做 ANN 近邻召回。向量索引按 **chunk**
建（命中 id 为 chunk 复合 id），故召回后按记录 ``metadata['unit_id']`` 归并到 unit
粒度（同 unit 多 chunk 取 MaxP），回传 ``unit.id``——使 UnitReader 能点读、Fuser 能
跨通道合并。query 无向量时返回空（该通道不参与）。
"""

from __future__ import annotations

from typing import List

from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from storage.types import VectorQuery
from storage.vector import VectorProducer, VectorStore

from .unit_aggregation import aggregate_to_units


class VectorRecaller(Recaller):
    """向量语义召回路（包一个 VectorStore）。"""

    def __init__(self, vector_store: VectorStore) -> None:
        self._vector = vector_store

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.VECTOR

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        if not query.vector:
            return []
        vq = VectorQuery(vector=query.vector, top_k=top_k, filters=query.scalar_filters)
        hits = self._vector.search(scope, vq)
        # 命中 id 为 chunk 复合 id；再点读记录拿回 metadata['unit_id']，归并到 unit 粒度。
        records = self._vector.get(scope, [h.id for h in hits])
        return aggregate_to_units(hits, records, RecallChannel.VECTOR)


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@RecallerProducer.register("vector")
def _build(config):
    # VectorStore 经 VectorProducer 自取（缺省 memory），与索引侧共享同一实例。
    return VectorRecaller(VectorProducer.dep(config, default="memory"))
