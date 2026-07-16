"""最小实现：:class:`~retrieval.recaller.Recaller` 的 KEYWORD 通道。

消费 ``ParsedQuery``，组装 :class:`~storage.types.TextQuery`（scope 落查询的专用
scope 入参做原生隔离、``scalar_filters`` 落 filters 做硬过滤），经注入的
:class:`~storage.fulltext.FulltextStore` 召回，再按记录 ``metadata['unit_id']`` 归并到
unit 粒度（与向量通道同一套口径，全文按 unit 建索引时为恒等映射）。多路并行与合并由
Retriever/Fuser 负责，本通道不感知其他通道。

支持 L0/L1 分层召回：``layer`` 参数（默认 "l2"=content 全文；"l0"/"l1"=预生成
概要/片段分表倒排）。store 为 None 时 recall 返空（该层未注入）。
"""

from __future__ import annotations

from typing import List

from common.log import get_logger
from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit
from storage.fulltext import FulltextProducer, FulltextStore
from storage.types import TextQuery

from .unit_aggregation import aggregate_to_units

logger = get_logger(__name__)


class KeywordRecaller(Recaller):
    """关键词/全文召回路（包一个 FulltextStore，支持 content/L0/L1 分层）。"""

    def __init__(self, fulltext: FulltextStore | None, *, layer: str = "l2") -> None:
        self._fulltext = fulltext
        self._layer = layer  # "l2"(content) | "l0" | "l1"

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    @property
    def layer(self) -> str:
        """当前召回层级（l2/l0/l1），公开只读。"""
        return self._layer

    @property
    def fulltext_store(self) -> FulltextStore | None:
        """注入的全文 store（只读；None 表示该层未注入，recall 返空）。"""
        return self._fulltext

    def channel(self) -> RecallChannel:
        return RecallChannel.KEYWORD

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> List[ScoredUnit]:
        if self._fulltext is None:
            return []  # store 未注入（该层未配）→ 跳过
        tq = TextQuery(
            text=query.rewritten or query.raw,
            top_k=top_k,
            filters=query.scalar_filters,  # scope 不混进 filters，单独走入参
        )
        hits = self._fulltext.search(scope, tq)
        records = self._fulltext.get(scope, [h.id for h in hits])
        result = aggregate_to_units(hits, records, RecallChannel.KEYWORD)
        logger.info(
            "KeywordRecaller: layer=%s scope=%s top_k=%d hits=%d units=%d",
            self._layer, scope, top_k, len(hits), len(result),
        )
        return result


# -- 注册到 RecallerProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@RecallerProducer.register("keyword")
def _build(config):
    return KeywordRecaller(FulltextProducer.dep(config, default="memory"), layer="l2")


@RecallerProducer.register("keyword_l0")
def _build_l0(config):
    # L0 分表 store：与构建侧同命名（layers_l0），经 FulltextProducer build_named 取具名实例。
    # layers_index_enabled 默认 true（与构建侧对齐：默认建默认查）；未配 layers_l0 → store=None
    # （recall 返空，不破坏其他路）。
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(None, layer="l0")
    ctx = config.ctx
    ns = ctx.namespaces.get(FulltextProducer.TOP_NAME, {})
    store = FulltextProducer.build_named("layers_l0", ctx) if "layers_l0" in ns else None
    if store is None:
        logger.info("KeywordRecaller(keyword_l0): store 未注入，recall 将返空")
    return KeywordRecaller(store, layer="l0")


@RecallerProducer.register("keyword_l1")
def _build_l1(config):
    if not config.get("layers_index_enabled", True):
        return KeywordRecaller(None, layer="l1")
    ctx = config.ctx
    ns = ctx.namespaces.get(FulltextProducer.TOP_NAME, {})
    store = FulltextProducer.build_named("layers_l1", ctx) if "layers_l1" in ns else None
    if store is None:
        logger.info("KeywordRecaller(keyword_l1): store 未注入，recall 将返空")
    return KeywordRecaller(store, layer="l1")

