# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一存储直写的 :class:`~construction.index_builder.IndexBuilder` 实现。

全部写入只经注入的 :class:`~storage.storage.Storage` 领域接口（``add``/``update``/
``delete``，``mode`` 原样透传）：一体化实现可在一次 ``add`` 内建立全部索引形式，
``CompositeStorage`` 只落记忆本体——覆盖范围由该实现按自身能力决定，本类不触碰
``storage.kv``/``vector``/``fulltext`` 等任何底层端口。

构建侧仍由本类完成两件事：

1. **向量化下传**：``vector_enabled=True`` 时走与
   :class:`~construction.index_builder_impl.vector_index_builder.VectorIndexBuilder`
   相同的管线（:class:`~common.chunker.base.Chunker` 切片 → 共享
   :class:`~common.embedder.base.Embedder` 逐 chunk embed，经 :mod:`._index_ops`
   的 ``vectorize_unit`` 共用实现），结果以 ``ChunkVector`` 列表挂到
   ``MemoryUnit.vectors`` 随本体一并下传（向量化必须在构建侧执行——与检索侧共用同一
   Embedder 实例才能保证同向量空间，Storage 层不持有插件）。
2. **索引过滤字段补齐**：把 ``FulltextIndexBuilder``/``VectorIndexBuilder`` 经
   ``_index_ops.index_metadata`` 单独投影的过滤字段（``content_layer``/``t_valid``/
   ``t_event``/``t_invalid`` 哨兵与 epoch 毫秒）直接写进 ``unit.system_metadata``，
   一体化后端从 ``system_metadata``/``user_metadata`` 直接读取建索引、不再需要
   ``index_metadata`` 投影下传。``seq`` 已在 ``ChunkVector`` 上，per-chunk 不重复。
"""

from __future__ import annotations

from jiuwen_memory.common.chunker.base import Chunker, ChunkerProducer
from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    T_EVENT_UNKNOWN,
    T_INVALID_OPEN,
    ChunkVector,
    MemoryUnit,
)
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from ._index_ops import group_units_by_scope, vectorize_unit

logger = get_logger(__name__)


class UnifiedIndexBuilder(IndexBuilder):
    """全部写委托 Storage 领域接口；``vector_enabled`` 时自建 chunk 级向量投影挂到 unit 上。"""

    def __init__(
        self,
        storage: Storage,
        *,
        vector_enabled: bool = True,
        chunker: Chunker | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        # 向量开关打开就必须有切片/向量化插件：缺了在装配期暴露，不拖到首次写入。
        if vector_enabled and (chunker is None or embedder is None):
            raise ValueError(
                "UnifiedIndexBuilder: vector_enabled=True 需要注入 chunker 与 embedder"
            )
        self._storage = storage
        self._vector_enabled = vector_enabled
        self._chunker = chunker
        self._embedder = embedder

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        """补齐索引过滤字段 + 向量化（开关门控）后按 Scope 分组委托 ``Storage.add``。"""
        self._enrich_index_metadata(units)
        self._maybe_vectorize(units)
        for scope, scoped_units in group_units_by_scope(units):
            self._storage.add(scope, scoped_units, mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """同 :meth:`build`：重补过滤字段 + 重新向量化后委托 ``Storage.update``。"""
        self._enrich_index_metadata(units)
        self._maybe_vectorize(units)
        for scope, scoped_units in group_units_by_scope(units):
            self._storage.update(scope, scoped_units, mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        """按 Scope 分组委托 ``Storage.delete``；``mode`` 原样下传，同 :meth:`build`。"""
        for scope, scoped_units in group_units_by_scope(units):
            self._storage.delete(scope, [unit.id for unit in scoped_units], mode=mode)

    def rebuild(self) -> None:
        # 最小实现：统一存储与真源同生命周期，无独立重建路径。
        return None

    def _enrich_index_metadata(self, units: list[MemoryUnit]) -> None:
        """把索引过滤投影字段直接补进 ``unit.system_metadata``，供一体化后端直接读取。

        复用 ``system_metadata``/``user_metadata`` 双命名空间承载过滤字段，一体化后端
        从中直接读取建索引，无需经 ``_index_ops.index_metadata`` 单独投影下传。
        补齐的字段（unit 顶层已有 ``unit_id``/``tier``/``lifecycle``/``tags``/
        ``entities``/``source``，后端直接读顶层；以下是不在顶层、需补的派生投影）：

        - ``content_layer``：内容索引恒为 ``"l2"``（L0/L1 分层记录由后端按需覆写）；
        - ``t_event``：epoch 毫秒，``None`` 落哨兵 ``T_EVENT_UNKNOWN``（恒写——
          否则事件窗下推按缺失字段排他，含时间词 query 对这批 unit 系统性空召回）；
        - ``t_valid``：epoch 毫秒，``None`` 不写（未生效记忆稀疏，下推用 LTE 放行即可）；
        - ``t_invalid``：epoch 毫秒，``None`` 落哨兵 ``T_INVALID_OPEN``（恒写——
          否则回溯查询最该命中的活跃记忆被排他）。

        哨兵与 ``memory_filter._field_value`` 的投影对称，使后置复核与下推不分叉。
        """
        for unit in units:
            sm = unit.system_metadata
            sm["content_layer"] = "l2"
            temporal = unit.temporal
            sm["t_event"] = (
                int(temporal.t_event.timestamp() * 1000)
                if temporal.t_event is not None
                else T_EVENT_UNKNOWN
            )
            if temporal.t_valid is not None:
                sm["t_valid"] = int(temporal.t_valid.timestamp() * 1000)
            sm["t_invalid"] = (
                int(temporal.t_invalid.timestamp() * 1000)
                if temporal.t_invalid is not None
                else T_INVALID_OPEN
            )

    def _maybe_vectorize(self, units: list[MemoryUnit]) -> None:
        """逐 unit 切片-embed 并回填 ``unit.vectors``；失败不阻断本体写入。

        本体是真源，向量是派生物——单个 unit embed 失败时其 ``vectors`` 留空，
        一体化后端可择时补算（与 VectorIndexBuilder 跳过该 unit 的容错水平一致）。
        """
        if not self._vector_enabled:
            return
        for unit in units:
            pairs = vectorize_unit(self._chunker, self._embedder, unit)
            unit.vectors = [
                ChunkVector(id=chunk.id, seq=chunk.seq, vector=vector)
                for chunk, vector in pairs
            ]


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("unified")
def _build(config):
    # 向量开关关闭时不解析 chunker/embedder：无向量链路的装配无需这两个插件。
    vector_enabled = config.get("vector_enabled", True)
    chunker = embedder = None
    if vector_enabled:
        chunker = ChunkerProducer.dep(config, default="fixed_window")
        embedder = EmbedderProducer.dep(config, default="hashing")
    return UnifiedIndexBuilder(
        StorageProducer.resolve(config),
        vector_enabled=vector_enabled,
        chunker=chunker,
        embedder=embedder,
    )
