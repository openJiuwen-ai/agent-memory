"""组合实现：:class:`~construction.index_builder.IndexBuilder`——倒排 + 向量双索引。

组合 :class:`FulltextIndexBuilder` 与 :class:`VectorIndexBuilder`，
对外统一提供 IndexBuilder 契约，内部委托给两个子 builder。
新增/修改索引逻辑只需改子 builder，Hybrid 只做编排。
"""

from __future__ import annotations

from typing import List

from common.chunker.base import Chunker, ChunkerProducer
from common.embedder.base import Embedder, EmbedderProducer
from common.log import get_logger
from common.type_def import MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.fulltext import FulltextProducer, FulltextStore
from storage.kv import KvProducer, KVStore
from storage.vector import VectorProducer, VectorStore

from .fulltext_index_builder import FulltextIndexBuilder
from .vector_index_builder import VectorIndexBuilder

logger = get_logger(__name__)


class HybridIndexBuilder(IndexBuilder):
    """同时维护倒排（全文）与向量两套索引——委托给子 builder。"""

    def __init__(
        self,
        fulltext: FulltextStore,
        vector: VectorStore,
        kv: KVStore,
        chunker: Chunker,
        embedder: Embedder,
    ) -> None:
        self._fulltext_builder = FulltextIndexBuilder(fulltext)
        self._vector_builder = VectorIndexBuilder(vector, kv, chunker, embedder)

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: List[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: building index for %d units", len(units))
        self._fulltext_builder.build(units)
        self._vector_builder.build(units)

    def update(self, units: List[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: updating index for %d units", len(units))
        self._fulltext_builder.update(units)
        self._vector_builder.update(units)

    def remove(self, unit_ids: List[str]) -> None:
        logger.info("HybridIndexBuilder: removing %d unit ids from index", len(unit_ids))
        self._fulltext_builder.remove(unit_ids)
        self._vector_builder.remove(unit_ids)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def remove_with_scope(self, unit_ids: List[str], scope: Scope) -> None:
        """已知 scope 时直接删除索引条目，避免 lookup。"""
        self._fulltext_builder.remove_with_scope(unit_ids, scope)
        self._vector_builder.remove_with_scope(unit_ids, scope)


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #




@IndexBuilderProducer.register("hybrid")
def _build(config):
    # 全文/向量 Store 与召回侧共享同一实例；embedder 与查询侧共享 → 同一向量空间。
    return HybridIndexBuilder(
        FulltextProducer.dep(config, default="memory"),
        VectorProducer.dep(config, default="memory"),
        KvProducer.dep(config, default="memory"),
        ChunkerProducer.dep(config, default="fixed_window"),
        EmbedderProducer.dep(config, default="hashing"),
    )
