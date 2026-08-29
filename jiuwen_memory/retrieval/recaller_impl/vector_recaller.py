"""最小实现：:class:`~retrieval.recaller.Recaller` 的 VECTOR 通道。

消费 ``ParsedQuery.vector``（由 QueryParser 经 Embedder 产出），组装
:class:`~storage.types.VectorQuery`（scope 走入参隔离、``scalar_filters`` 走硬过滤），
经注入的 :class:`~storage.vector.VectorStore` 做 ANN 近邻召回。向量索引按 **chunk**
建（命中 id 为 chunk 复合 id），故召回后按记录 ``metadata['unit_id']`` 归并到 unit
粒度（同 unit 多 chunk 取 MaxP），回传 ``unit.id``——使 UnitReader 能点读、Fuser 能
跨通道合并。query 无向量时返回空（该通道不参与）。

支持 L0/L1 分层召回：``layer`` 参数（默认 "l2"=content 全文 chunk；"l0"/"l1"=预生成
概要/片段整段向量，查对应分表 store）。store 为 None 时 recall 返空（该层未注入）。
"""

from __future__ import annotations

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.recaller import Recaller, RecallerProducer
from jiuwen_memory.retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import VectorQuery
from jiuwen_memory.storage.vector import VectorStore

from .unit_aggregation import aggregate_to_units

logger = get_logger(__name__)


class VectorRecaller(Recaller):
    """向量语义召回路（包一个 VectorStore，支持 content/L0/L1 分层）。"""

    def __init__(
        self,
        storage: Storage,
        min_similarity: float = 0.0,
        *,
        layer: str = "l2",
    ) -> None:
        port_name = "default" if layer == "l2" else f"layers_{layer}"
        self._vector = (
            storage.vector_port(port_name) if storage.has_vector_port(port_name) else None
        )
        # 语义前置阈值：相似度低于此值的命中直接丢弃（默认 0 关闭）。
        self._min_similarity = float(min_similarity)
        self._layer = layer  # "l2"(content) | "l0" | "l1"
        # 召回后的 chunk→unit MaxP、分层 MaxP 与融合排序统一采用「分越大越相关」
        # 语义。距离型度量（如 L2，越小越相关）即使关闭 min_similarity，也会在
        # MaxP/降序排序时反转相关性，因此装配期统一拒绝。
        if self._vector is not None and not self._vector.score_higher_is_better():
            raise ValidationError(
                "向量召回链路要求「分越大越相关」的度量（cosine/IP）；"
                "当前向量库声明为距离型（越小越相关，如 L2），"
                "无法用于 MaxP 聚合与降序融合"
            )

    @property
    def layer(self) -> str:
        """当前召回层级（l2/l0/l1），公开只读。"""
        return self._layer

    @property
    def vector_store(self) -> VectorStore | None:
        """注入的向量 store（只读；None 表示该层未注入，recall 返空）。"""
        return self._vector

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.VECTOR

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        if self._vector is None or not query.vector:
            return []  # store 未注入（该层未配）或无 query 向量 → 跳过
        vq = VectorQuery(
            vector=query.vector,
            top_k=top_k,
            filters=query.scalar_filters,
            extensions=dict(query.extensions),
        )
        # 优先走 store.recall：在召回请求内一并回带 metadata，省掉再发一次 get 的
        # 网络 RTT 与服务端 id 匹配开销（远端后端如 milvus 收益显著）。store 未实现
        # recall 时抛 NotImplementedError，回退到 search + get 两段式（内存后端零成本）。
        try:
            hits = self._vector.recall(scope, vq, output_fields=["metadata"])
        except NotImplementedError:
            scored = self._vector.search(scope, vq)
            # 语义前置阈值：融合前先砍掉明显不相关的语义命中，省下游 recheck/rerank 预算。
            if self._min_similarity > 0.0:
                scored = [s for s in scored if s.score >= self._min_similarity]
            # 命中 id 为 chunk 复合 id（L2）或 {unit_id}-l0/l1（L0/L1）；点读记录拿回
            # metadata['unit_id']，归并到 unit 粒度（同 unit 多 record 取 MaxP）。
            records = self._vector.get(scope, [s.id for s in scored])
            result = aggregate_to_units(scored, records, RecallChannel.VECTOR)
            logger.info(
                "VectorRecaller(fallback search+get): layer=%s scope=%s "
                "top_k=%d hits=%d units=%d",
                self._layer, scope, top_k, len(scored), len(result),
            )
            return result
        # recall 路径下 metadata 已在 hits 内，records 即 hits 自身。
        if self._min_similarity > 0.0:
            hits = [h for h in hits if h.score >= self._min_similarity]
        result = aggregate_to_units(hits, hits, RecallChannel.VECTOR)
        logger.info(
            "VectorRecaller: layer=%s scope=%s top_k=%d hits=%d units=%d",
            self._layer, scope, top_k, len(hits), len(result),
        )
        return result


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("vector")
def _build(config):
    return VectorRecaller(
        StorageProducer.resolve(config),
        min_similarity=float(Factory.cfg_get(config, "min_similarity", 0.0)),
        layer="l2",
    )


@RecallerProducer.register("vector_l0")
def _build_l0(config):
    # layers_index_enabled 默认 true；未配置命名端口时该层返回空结果。
    if not config.get("layers_index_enabled", True):
        return VectorRecaller(StorageProducer.resolve(config), layer="l0")
    recaller = VectorRecaller(StorageProducer.resolve(config), layer="l0")
    if recaller.vector_store is None:
        logger.info("VectorRecaller(vector_l0): store 未注入，recall 将返空")
    return recaller


@RecallerProducer.register("vector_l1")
def _build_l1(config):
    if not config.get("layers_index_enabled", True):
        return VectorRecaller(StorageProducer.resolve(config), layer="l1")
    recaller = VectorRecaller(StorageProducer.resolve(config), layer="l1")
    if recaller.vector_store is None:
        logger.info("VectorRecaller(vector_l1): store 未注入，recall 将返空")
    return recaller
