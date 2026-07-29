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
from storage.fulltext import FulltextProducer, FulltextStore
from storage.kv import KvProducer, KVStore
from storage.vector import VectorProducer, VectorStore

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
        fulltext: FulltextStore,
        vector: VectorStore,
        kv: KVStore,
        chunker: Chunker,
        embedder: Embedder,
        fulltext_l0: FulltextStore | None = None,
        fulltext_l1: FulltextStore | None = None,
        vector_l0: VectorStore | None = None,
        vector_l1: VectorStore | None = None,
    ) -> None:
        self._fulltext_builder = FulltextIndexBuilder(
            fulltext,
            fulltext_l0=fulltext_l0,
            fulltext_l1=fulltext_l1,
        )
        self._vector_builder = VectorIndexBuilder(
            vector,
            kv,
            chunker,
            embedder,
            vector_l0=vector_l0,
            vector_l1=vector_l1,
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
    # 全文/向量 Store 与召回侧共享同一实例；embedder 与查询侧共享 → 同一向量空间。
    # L0/L1 分层索引：layers_index_enabled（默认 true）开时，取 vector_store_l0/l1、
    # fulltext_store_l0/l1 具名实例（指向不同 collection/index = 分表）注入子 builder；
    # 具名实例未配置则该层传 None，子 builder 跳过该层（向后兼容 + 配置降级）。
    layers_enabled = config.get("layers_index_enabled", True)

    def _opt_dep(producer_cls, name):
        """取具名实例；未配置则返回 None（不报错，子 builder 跳过该层）。

        直接走 ``build_named``（从 ctx.namespaces 按名取具名实例），不走 ``dep``——
        ``dep`` 从当前组件 params 取字段，而 layers_l0 是 store 命名空间下的具名实例名，
        不在 index_builder 的 params 里。
        """
        if not layers_enabled:
            return None
        ctx = config.ctx
        ns = ctx.namespaces.get(producer_cls.TOP_NAME, {})
        if name not in ns:
            return None  # 该层具名实例未声明，跳过
        return producer_cls.build_named(name, ctx)

    return HybridIndexBuilder(
        FulltextProducer.dep(config, default="memory"),
        VectorProducer.dep(config, default="memory"),
        KvProducer.dep(config, default="memory"),
        ChunkerProducer.dep(config, default="fixed_window"),
        EmbedderProducer.dep(config, default="hashing"),
        fulltext_l0=_opt_dep(FulltextProducer, "layers_l0"),
        fulltext_l1=_opt_dep(FulltextProducer, "layers_l1"),
        vector_l0=_opt_dep(VectorProducer, "layers_l0"),
        vector_l1=_opt_dep(VectorProducer, "layers_l1"),
    )
