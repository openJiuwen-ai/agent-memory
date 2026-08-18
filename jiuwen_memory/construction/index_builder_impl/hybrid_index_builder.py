"""组合实现：:class:`~construction.index_builder.IndexBuilder`——倒排 + 向量双索引。

组合 :class:`FulltextIndexBuilder` 与 :class:`VectorIndexBuilder`，
对外统一提供 IndexBuilder 契约，内部委托给两个子 builder。
新增/修改索引逻辑只需改子 builder，Hybrid 只做编排。
"""

from __future__ import annotations

from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer

from .entity_index_builder import EntityIndexBuilder, EntityIndexAdmissionPolicy, EntityLinkService
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
        entity_linker: EntityLinkService | None = None,
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
        # entity 子 builder：None 表示不建实体索引（entity_enabled=False 或未注入 linker）
        self._entity_builder = EntityIndexBuilder(entity_linker) if entity_linker is not None else None

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: building index for %d units", len(units))
        self._fulltext_builder.build(units)
        self._vector_builder.build(units)
        if self._entity_builder is not None:
            self._entity_builder.build(units)

    def update(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: updating index for %d units", len(units))
        self._fulltext_builder.update(units)
        self._vector_builder.update(units)
        if self._entity_builder is not None:
            self._entity_builder.update(units)

    def remove(self, units: list[MemoryUnit]) -> None:
        logger.info("HybridIndexBuilder: removing %d units from index", len(units))
        self._fulltext_builder.remove(units)
        self._vector_builder.remove(units)
        if self._entity_builder is not None:
            self._entity_builder.remove(units)

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
        if self._entity_builder is not None:
            self._entity_builder.remove_with_scope(unit_ids, scope)


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #




@IndexBuilderProducer.register("hybrid")
def _build(config):


    # entity_linker 注入：entity_enabled 默认 False（需显式开启 + EntityStore 后端
    # 可解析）
    entity_linker = None
    if config.get("entity_enabled", False):
        try:
            from jiuwen_memory.storage.entity_store import EntityStoreProducer

            entity_store = EntityStoreProducer.dep(config, default="elasticsearch")
            if entity_store is not None:
                entity_linker = EntityLinkService(
                    entity_store=entity_store,
                    admission_policy=EntityIndexAdmissionPolicy(),
                )
        except Exception as exc: 
            logger.warning(
                "EntityStore 装配失败, entity 链路降级关闭(fulltext+vector 继续工作): %s",
                exc,
                exc_info=True,
            )
            entity_linker = None

    return HybridIndexBuilder(
        StorageProducer.resolve(config),
        ChunkerProducer.dep(config, default="fixed_window"),
        EmbedderProducer.dep(config, default="hashing"),
        layers_enabled=config.get("layers_index_enabled", True),
        entity_linker=entity_linker,
    )
