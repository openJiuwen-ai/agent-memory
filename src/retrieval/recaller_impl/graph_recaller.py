"""最小实现：:class:`~retrieval.recaller.Recaller` 的 GRAPH 通道。

图召回靠多跳：先在图里按 query 关键词找种子节点（``InMemoryGraphStore.seed_ids``），
再 BFS 扩展其邻居，把**关联到的**记忆单元作为候选返回——补充关键词/向量直接命中
之外、靠关系「连点成线」找到的相关记忆。图为空（尚未 ASSOCIATE）时返回空。
"""

from __future__ import annotations

from typing import Dict, List

from common.factory.factory import Factory
from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from storage.graph import GraphProducer, GraphStore
from storage.types import GraphQuery


class GraphRecaller(Recaller):
    """
    图遍历召回路：关键词种子 → 1 跳邻居作为关联候选。仅依赖抽象
    :class:`~storage.graph.GraphStore`（``seed_ids`` + ``search``），不绑定具体后端。
    """

    def __init__(self, graph: GraphStore, depth: int = 1) -> None:
        self._graph = graph
        self._depth = depth

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.GRAPH

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        # 种子词项 = 关键词 ∪ 实体文本（实体更精准地定位图入口）。
        terms = set(query.keywords) | {e.text for e in query.entities if e.text}
        seeds = self._graph.seed_ids(scope, terms)
        scores: Dict[str, float] = {}
        for seed in seeds:
            for node in self._graph.search(
                scope, GraphQuery(start_id=seed, depth=self._depth, limit=top_k)
            ):
                scores[node.id] = max(scores.get(node.id, 0.0), 1.0)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [ScoredUnit(unit_id=uid, score=s, channel=RecallChannel.GRAPH) for uid, s in ranked]


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("graph")
def _build(config):
    # GraphStore 经 GraphProducer 自取并共享同一实例；遍历跳数 depth 可经 params 覆盖。
    return GraphRecaller(
        GraphProducer.dep(config, default="memory"),
        depth=Factory.cfg_get(config, "depth", 1),
    )
