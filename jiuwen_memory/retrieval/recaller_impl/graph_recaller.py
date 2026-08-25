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
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import GraphQuery


class GraphRecaller(Recaller):
    """
    图遍历召回路：关键词种子 → 1 跳邻居作为关联候选。仅依赖抽象
    :class:`~storage.graph.GraphStore`（``seed_ids`` + ``search``），不绑定具体后端。
    """

    def __init__(self, storage: Storage, depth: int = 1) -> None:
        """初始化 GraphRecaller。

        Args:
            storage: 参数 storage（Storage）。
            depth: 参数 depth（int）。
        """
        self._graph = storage.graph
        self._depth = depth

    def operator_type(self) -> RetrievalOperatorType:
        """返回当前算子类型。

        Returns:
            返回 RetrievalOperatorType。
        """
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        """执行健康检查。"""
        return None

    def channel(self) -> RecallChannel:
        """执行 `channel` 操作。

        Returns:
            返回 RecallChannel。
        """
        return RecallChannel.GRAPH

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        # 种子词项 = 关键词 ∪ 实体文本（实体更精准地定位图入口）。
        """召回与查询匹配的记忆结果。

        Args:
            scope: 参数 scope（Scope）。
            query: 参数 query（ParsedQuery）。
            top_k: 参数 top_k（int）。

        Returns:
            返回 list[ScoredUnit]。
        """
        terms = set(query.keywords) | {e.text for e in query.entities if e.text}
        seeds = self._graph.seed_ids(scope, terms)
        scores: dict[str, float] = {}
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
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    return GraphRecaller(
        StorageProducer.resolve(config),
        depth=Factory.cfg_get(config, "depth", 1),
    )
