"""组合实现：:class:`~construction.index_builder.IndexBuilder`。

组合 :class:`ForwardIndexBuilder`、:class:`FulltextIndexBuilder`、
:class:`VectorIndexBuilder` 与 :class:`EntityIndexBuilder`，对外统一提供 IndexBuilder
契约，内部委托给四个子 builder（entity 子 builder 在未注入 linker 时为 None，整条链
跳过）。新增/修改索引逻辑只需改子 builder，Hybrid 只做编排、不直接碰任何 Store。

记忆写入以本算子为唯一入口。一个子 builder 只负责一种索引形式，各自的 Store 端口
统一从注入的 ``Storage`` 取——与读侧 recaller 取自同一个 Storage 实例的同一端口，
读写不分叉。调用方不再自行调 Storage 的写接口。

本算子只执行被要求的索引操作，不解读 ``unit.lifecycle``：记忆处于什么状态、因而该对
索引做什么，由上层判定后调对应方法。如归档/遗忘，上层先 ``update`` 回写本体新状态，
再 ``remove(mode=SOFT)`` 让它退出检索。
"""

from __future__ import annotations

from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from .entity_index_builder import EntityIndexAdmissionPolicy, EntityIndexBuilder, EntityLinkService
from .forward_index_builder import ForwardIndexBuilder
from .fulltext_index_builder import FulltextIndexBuilder
from .vector_index_builder import VectorIndexBuilder

logger = get_logger(__name__)


class HybridIndexBuilder(IndexBuilder):
    """同时维护正排、倒排（全文）、向量与实体反向四套索引——委托给子 builder。

    L0/L1 分层 store 透传给倒排与向量两个子 builder：非 None 时子 builder 会为带 layers
    的 unit 构建分层索引（写独立 store = 分表）；None 时子 builder 跳过 L0/L1，只走 content。
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
        # 各子 builder 的 Store 端口都从这一个 storage 取，保证读写同源。
        self._forward_builder = ForwardIndexBuilder(storage)
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

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        logger.info(
            "HybridIndexBuilder: building index for %d units (mode=%s)",
            len(units), mode,
        )
        # 顺序约定：正排最先出现——检索索引写失败时本体仍在、可重建。
        # 各子 builder 自行按 mode 跳过不属于自己的索引形式。
        self._forward_builder.build(units, mode=mode)
        self._fulltext_builder.build(units, mode=mode)
        self._vector_builder.build(units, mode=mode)
        if self._entity_builder is not None:
            self._entity_builder.build(units, mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """更新四种索引形式；``mode`` 限定本次只写正排本体或只写检索索引。

        本算子不解读 ``unit.lifecycle``——记忆处于什么状态、该对索引做什么操作，由上层
        判定后直接调对应方法（要移出检索就再调一次 ``remove(mode=SOFT)``）。
        """
        logger.info(
            "HybridIndexBuilder: updating index for %d units (mode=%s)",
            len(units), mode,
        )
        self._forward_builder.update(units, mode=mode)
        self._fulltext_builder.update(units, mode=mode)
        self._vector_builder.update(units, mode=mode)
        if self._entity_builder is not None:
            self._entity_builder.update(units, mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """删除索引；``mode=HARD`` 时连记忆本体一并物理删除。

        归档/遗忘等非破坏式治理传 ``SOFT``——记忆本体由 LifecycleManager 改状态后
        保留，此处只让它退出检索（search/recall 不再召回，``get``/``list`` 仍可读）。
        """
        logger.info(
            "HybridIndexBuilder: removing %d units from index (mode=%s)",
            len(units), mode,
        )
        # 顺序约定：正排最后消失——检索索引先移除，中途失败时记忆本体仍在、可重试补删。
        # 各子 builder 自行按 mode 决定动作（正排在 SOFT 下保留本体）。
        self._fulltext_builder.remove(units, mode=mode)
        self._vector_builder.remove(units, mode=mode)
        if self._entity_builder is not None:
            self._entity_builder.remove(units, mode=mode)
        self._forward_builder.remove(units, mode=mode)

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
