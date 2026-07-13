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

from common.errors import ValidationError
from common.factory.factory import Factory
from common.log import get_logger
from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from storage.types import VectorQuery
from storage.vector import VectorProducer, VectorStore

from .unit_aggregation import aggregate_to_units

logger = get_logger(__name__)


class VectorRecaller(Recaller):
    """向量语义召回路（包一个 VectorStore，支持 content/L0/L1 分层）。"""

    def __init__(
        self,
        vector_store: VectorStore | None,
        min_similarity: float = 0.0,
        *,
        layer: str = "l2",
    ) -> None:
        self._vector = vector_store
        # 语义前置阈值：相似度低于此值的命中直接丢弃（默认 0 关闭）。
        self._min_similarity = float(min_similarity)
        self._layer = layer  # "l2"(content) | "l0" | "l1"
        # 装配期防呆：距离型度量（如 L2，越小越相关）下启用该过滤会静默砍掉最相关
        # 候选，按 store 声明的分数方向直接拒绝（fail-fast，部署起不来而非静默劣化）。
        if self._vector is not None and self._min_similarity > 0.0 and not vector_store.score_higher_is_better():
            raise ValidationError(
                "向量召回 min_similarity 需要「分越大越相关」的度量（cosine/IP），"
                "当前向量库声明分数为距离型（越小越相关，如 L2），不可启用语义前置阈值"
            )

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    @property
    def layer(self) -> str:
        """当前召回层级（l2/l0/l1），公开只读。"""
        return self._layer

    @property
    def vector_store(self) -> VectorStore | None:
        """注入的向量 store（只读；None 表示该层未注入，recall 返空）。"""
        return self._vector

    def channel(self) -> RecallChannel:
        return RecallChannel.VECTOR

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        if self._vector is None or not query.vector:
            return []  # store 未注入（该层未配）或无 query 向量 → 跳过
        vq = VectorQuery(vector=query.vector, top_k=top_k, filters=query.scalar_filters)
        hits = self._vector.search(scope, vq)
        # 语义前置阈值：融合前先砍掉明显不相关的语义命中，省下游 recheck/rerank 预算。
        if self._min_similarity > 0.0:
            hits = [h for h in hits if h.score >= self._min_similarity]
        # 命中 id 为 chunk 复合 id（L2）或 {unit_id}-l0/l1（L0/L1）；点读记录拿回
        # metadata['unit_id']，归并到 unit 粒度（同 unit 多 record 取 MaxP）。
        records = self._vector.get(scope, [h.id for h in hits])
        result = aggregate_to_units(hits, records, RecallChannel.VECTOR)
        logger.info(
            "VectorRecaller: layer=%s scope=%s top_k=%d hits=%d units=%d",
            self._layer, scope, top_k, len(hits), len(result),
        )
        return result


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("vector")
def _build(config):
    # VectorStore 经 VectorProducer 自取（缺省 memory），与索引侧共享同一实例。
    return VectorRecaller(
        VectorProducer.dep(config, default="memory"),
        min_similarity=float(Factory.cfg_get(config, "min_similarity", 0.0)),
        layer="l2",
    )


@RecallerProducer.register("vector_l0")
def _build_l0(config):
    # L0 分表 store：与构建侧同命名（layers_l0），经 VectorProducer build_named 取具名实例。
    # layers_index_enabled 默认 true（与构建侧对齐：默认建默认查）；未配 layers_l0 → store=None
    # （recall 返空，不破坏其他路）。
    if not config.get("layers_index_enabled", True):
        return VectorRecaller(None, layer="l0")
    ctx = config.ctx
    ns = ctx.namespaces.get(VectorProducer.TOP_NAME, {})
    store = VectorProducer.build_named("layers_l0", ctx) if "layers_l0" in ns else None
    if store is None:
        logger.info("VectorRecaller(vector_l0): store 未注入，recall 将返空")
    return VectorRecaller(store, layer="l0")


@RecallerProducer.register("vector_l1")
def _build_l1(config):
    if not config.get("layers_index_enabled", True):
        return VectorRecaller(None, layer="l1")
    ctx = config.ctx
    ns = ctx.namespaces.get(VectorProducer.TOP_NAME, {})
    store = VectorProducer.build_named("layers_l1", ctx) if "layers_l1" in ns else None
    if store is None:
        logger.info("VectorRecaller(vector_l1): store 未注入，recall 将返空")
    return VectorRecaller(store, layer="l1")

