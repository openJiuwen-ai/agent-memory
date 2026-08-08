"""组合实现：:class:`~construction.index_builder.IndexBuilder`——倒排 + 向量双索引。

组合 :class:`FulltextIndexBuilder` 与 :class:`VectorIndexBuilder`，
对外统一提供 IndexBuilder 契约，内部委托给两个子 builder。
新增/修改索引逻辑只需改子 builder，Hybrid 只做编排。
"""

from __future__ import annotations

from common.chunker.base import Chunker, ChunkerProducer
from common.embedder.base import Embedder, EmbedderProducer
from common.log import get_logger
from common.type_def import MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.storage import Storage, StorageProducer

from .fulltext_index_builder import FulltextIndexBuilder
from .vector_index_builder import VectorIndexBuilder

logger = get_logger(__name__)


class HybridIndexBuilder(IndexBuilder):
    """同时维护倒排（全文）与向量两套索引——委托给子 builder。

    L0/L1 分层 store 透传给两个子 builder：非 None 时子 builder 会为带 layers 的 unit
    构建分层索引（写独立 store = 分表）；None 时子 builder 跳过 L0/L1，只走 content。
    """

    def __init__(
        self,
        storage: Storage,
        chunker: Chunker,
        embedder: Embedder,
        *,
        layers_enabled: bool = True,
    ) -> None:
        self._fulltext_builder = FulltextIndexBuilder(
            storage,
            layers_enabled=layers_enabled,
        )
        self._vector_builder = VectorIndexBuilder(
            storage,
            chunker,
            embedder,
            layers_enabled=layers_enabled,
        )

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: building index for %d units", len(units))
        self._fulltext_builder.build(units)
        self._vector_builder.build(units)

    def update(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: updating index for %d units", len(units))
        self._fulltext_builder.update(units)
        self._vector_builder.update(units)

    def remove(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: removing %d units from index", len(units))
        self._fulltext_builder.remove(units)
        self._vector_builder.remove(units)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        """已知 scope 时直接删除索引条目，避免 lookup。"""
        self._fulltext_builder.remove_with_scope(unit_ids, scope)
        self._vector_builder.remove_with_scope(unit_ids, scope)


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #




@IndexBuilderProducer.register("hybrid")
def _build(config):
    return HybridIndexBuilder(
        StorageProducer.resolve(config),
        ChunkerProducer.dep(config, default="fixed_window"),
        EmbedderProducer.dep(config, default="hashing"),
        layers_enabled=config.get("layers_index_enabled", True),
    )
