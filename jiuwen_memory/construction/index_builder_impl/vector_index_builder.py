# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 经注入的 :class:`~common.chunker.base.Chunker` 切片
→ :class:`~common.embedder.base.Embedder` 向量化 → 写入
:class:`~storage.vector.VectorStore`（向量 ANN 索引）。
同时在 :class:`~storage.kv.KVStore` 维护 chunk_id 跟踪记录（update/remove 时
读取旧 chunk_id 列表）。删除入口接收 ``MemoryUnit``，直接使用其 scope 定位索引。

VectorRecord.id 采用 ``{unit.id}-{chunk.id}`` 拼接格式，在记录所属 Scope 内唯一。

切片、向量化、分层索引等投影流水线与其他 IndexBuilder 实现共用
:mod:`._index_ops`，本类只做批量编排与容错顺序。
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
from jiuwen_memory.storage.vector import VectorStore

from ._index_ops import (
    build_layer_vector_indexes,
    clear_unit_vector_index,
    vector_port,
    vectorize_units,
    write_chunk_trackings,
    write_vector_index,
)

logger = get_logger(__name__)


class VectorIndexBuilder(IndexBuilder):
    """向量索引构建：MemoryUnit → Chunker → Embedder → VectorStore + chunk tracking。

    L0/L1 分层索引（架构 §9.1）：``unit.layers.l0``/``.l1`` 非空且对应 store 已注入时，
    对整段 l0/l1 文本（不切片）做 embed，写独立 VectorStore 实例（不同 collection = 分表），
    record id = ``{unit_id}-layer-l0``/``{unit_id}-layer-l1``。store 为 None 时跳过该层，
    不影响 content。
    """

    def __init__(
        self,
        storage: Storage,
        chunker: Chunker,
        embedder: Embedder,
        *,
        layers_enabled: bool = True,
    ) -> None:
        self._vector_store = storage.vector if storage.has_vector() else None
        self._kv_store = storage.kv if storage.has_kv() else None
        self._chunker = chunker
        self._embedder = embedder
        # L0/L1 分层 store：None 表示不构建该层索引（向后兼容 + 配置降级）。
        self._vector_l0 = vector_port(storage, "layers_l0", layers_enabled)
        self._vector_l1 = vector_port(storage, "layers_l1", layers_enabled)

    @property
    def vector_l0(self) -> VectorStore | None:
        """L0 分层 store（只读；None 表示该层未注入，recall/构建跳过）。"""
        return self._vector_l0

    @property
    def vector_l1(self) -> VectorStore | None:
        """L1 分层 store（只读；None 表示该层未注入）。"""
        return self._vector_l1

    def _layer_ports(self) -> tuple[tuple[VectorStore | None, str], ...]:
        return ((self._vector_l0, "l0"), (self._vector_l1, "l1"))

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    # ------------------------------------------------------------------
    # IndexBuilder 契约
    # ------------------------------------------------------------------

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        """为一批记忆单元构建向量索引。"""
        # 本实现只建检索索引，不交付记忆本体：FORWARD_ONLY 即整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        logger.info("VectorIndexBuilder: building index for %d units", len(units))
        if self._vector_store is None:
            self._build_layers(units)
            return
        scope_groups, chunk_tracking = vectorize_units(self._chunker, self._embedder, units)

        # L0/L1 分层索引：store 非空且 layers 非空才整段 embed 写独立 store（分表）。
        # 必须在下方空结果判断之前执行——content 切不出 chunk 时没有 L2 record，
        # 但 unit 的 layers.l0/l1 仍可能非空、仍需建分层索引（update
        # 路径亦依赖此处按新 layers 重建，见 update 的删旧→重建约定）。
        self._build_layers(units)

        if not scope_groups:
            return

        write_vector_index(self._vector_store, scope_groups)

        # chunk_id 跟踪写入 KVStore
        if self._kv_store is None:
            return
        write_chunk_trackings(self._kv_store, chunk_tracking)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """增量更新向量索引：先删旧 chunk + 旧 L0/L1 record → 再建新。

        SUPERSEDE/UPDATE 场景下 unit 可能从「有 layers」变「无 layers」（或反之），故 L0/L1
        必须先删旧 record（store 非空才删），再由 build 按新 layers 决定是否重建——否则
        旧 L0/L1 残留。
        """
        # 本实现只建向量索引（检索）：调用方要求只动正排时整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        # 先删旧 chunk（跟踪记录保留待覆写）+ 旧 L0/L1 record（幂等）
        for unit in units:
            clear_unit_vector_index(
                self._kv_store,
                self._vector_store,
                self._layer_ports(),
                unit.scope,
                unit.id,
                clear_tracking=False,
            )
        if self._vector_store is None or self._kv_store is None:
            self._build_layers(units)
            return
        self.build(units)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """删除一批记忆单元对应的向量索引条目（幂等）。

        本实现只持有检索索引：SOFT/HARD 都要移出检索，行为相同。
        """
        for unit in units:
            self._remove_by_scope(unit.id, unit.scope)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        """已知 scope 时直接删除索引条目，避免 lookup。"""
        for unit_id in unit_ids:
            self._remove_by_scope(unit_id, scope)

    def _remove_by_scope(self, unit_id: str, scope: Scope) -> None:
        """删除单个 unit 的向量索引条目 + chunk tracking + L0/L1 record。"""
        clear_unit_vector_index(
            self._kv_store,
            self._vector_store,
            self._layer_ports(),
            scope,
            unit_id,
            clear_tracking=True,
        )

    def _build_layers(self, units: list[MemoryUnit]) -> None:
        """对带 layers 的 unit 构建 L0/L1 向量索引（整段 embed，写独立 store）。"""
        build_layer_vector_indexes(self._layer_ports(), self._embedder, units)


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("vector")
def _build(config):
    return VectorIndexBuilder(
        storage=StorageProducer.resolve(config),
        chunker=ChunkerProducer.dep(config, default="fixed_window"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        layers_enabled=config.get("layers_index_enabled", True),
    )
