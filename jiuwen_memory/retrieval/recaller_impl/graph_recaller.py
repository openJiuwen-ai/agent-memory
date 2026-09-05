# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~retrieval.recaller.Recaller` 的 GRAPH 通道。

图召回靠多跳：先在图里按 query 关键词找种子节点（``InMemoryGraphStore.seed_ids``），
再 BFS 扩展其邻居，把**关联到的**记忆单元作为候选返回——补充关键词/向量直接命中
之外、靠关系「连点成线」找到的相关记忆。图为空（尚未 ASSOCIATE）时返回空。
"""

from __future__ import annotations

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.recaller import Recaller, RecallerProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import GraphQuery


class GraphRecaller(Recaller):
    """
    图遍历召回路：关键词种子 → 1 跳邻居作为关联候选。仅依赖抽象
    :class:`~storage.graph.GraphStore`（``seed_ids`` + ``search``），不绑定具体后端。
    """

    def __init__(
        self, storage: StoreManager, depth: int = 1, *, graph_name: str = "default"
    ) -> None:
        # graph 端口是可选依赖：未声明该端口的 manager（最小装配）下 store 为
        # None，recall 返空——与 vector/keyword recaller 的 store None 约定一致。
        self._graph = (
            storage.graph(graph_name) if storage.has_graph(graph_name) else None
        )
        self._depth = depth

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.GRAPH

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        if self._graph is None:
            return []  # store 未注入（该端口未配）→ 跳过
        # 种子词项 = 关键词 ∪ 实体文本（实体更精准地定位图入口）。
        terms = set(query.keywords) | {e.text for e in query.entities if e.text}
        seeds = self._graph.seed_ids(scope, terms)
        scores: dict[str, float] = {}
        for seed in seeds:
            for node in self._graph.search(
                scope,
                GraphQuery(
                    start_id=seed,
                    depth=self._depth,
                    limit=top_k,
                    extensions=dict(query.extensions),
                ),
            ):
                scores[node.id] = max(scores.get(node.id, 0.0), 1.0)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [ScoredUnit(unit_id=uid, score=s, channel=RecallChannel.GRAPH) for uid, s in ranked]


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("graph")
def _build(config):
    return GraphRecaller(
        StoreManagerProducer.resolve(config),
        depth=Factory.cfg_get(config, "depth", 1),
        graph_name=resolve_name(config, "graph_store"),
    )
