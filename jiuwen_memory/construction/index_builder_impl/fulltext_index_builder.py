# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 写入注入的 :class:`~storage.fulltext.FulltextStore`
（hot 轻量索引）。删除入口接收 ``MemoryUnit``，直接使用其 scope 定位索引。

metadata 投影、文档构造与分层索引写删与其他 IndexBuilder 实现共用
:mod:`._index_ops`，本类只做批量编排与容错顺序。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger, metadata_for_log, redact_for_log
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

from ._index_ops import (
    build_fulltext_layers,
    content_document,
    delete_layer_documents,
    fulltext_port,
)

logger = get_logger(__name__)


class FulltextIndexBuilder(IndexBuilder):
    """把记忆单元 content 写入全文索引（hot 轻量索引）。

    L0/L1 分层索引（架构 §9.1）：``unit.layers.l0``/``.l1`` 非空且对应 store 已注入时，
    写独立 FulltextStore 实例（不同 index = 分表），document id = ``{unit_id}:l0``/
    ``{unit_id}:l1``。store 为 None 时跳过该层，不影响 content。
    """

    def __init__(
        self,
        storage: Storage,
        *,
        layers_enabled: bool = True,
    ) -> None:
        self._store = storage.fulltext if storage.has_fulltext() else None
        self._fulltext_l0 = fulltext_port(storage, "layers_l0", layers_enabled)
        self._fulltext_l1 = fulltext_port(storage, "layers_l1", layers_enabled)

    @property
    def fulltext_l0(self) -> FulltextStore | None:
        """L0 分层 store（只读；None 表示该层未注入，构建跳过）。"""
        return self._fulltext_l0

    @property
    def fulltext_l1(self) -> FulltextStore | None:
        """L1 分层 store（只读；None 表示该层未注入）。"""
        return self._fulltext_l1

    def _layer_ports(self) -> tuple[tuple[FulltextStore | None, str], ...]:
        return ((self._fulltext_l0, "l0"), (self._fulltext_l1, "l1"))

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        # 本实现只建检索索引，不交付记忆本体：FORWARD_ONLY 即整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        logger.info("FulltextIndexBuilder: building index for %d units", len(units))
        for unit in units:
            if self._store is not None:
                logger.info(
                    "FulltextIndexBuilder: indexing unit id=%s tier=%s tags=%s content=%s",
                    unit.id[:8],
                    unit.tier.value,
                    metadata_for_log(unit.tags),
                    redact_for_log(unit.content),
                )
                self._store.insert(unit.scope, [content_document(unit)])
            # L0/L1 分层：store 非空且 layers 非空才写独立 store（分表）
            self._build_layers(unit)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        """删后重建，与 :class:`VectorIndexBuilder` 同一容错水平。

        不用 ``store.update``——它要求文档已存在，而文档可能先前已被移出检索（如归档后
        再更新），那时会抛 ``NotFoundError``。``delete`` 契约幂等，删后 ``insert`` 对
        「已存在」与「不存在」两种前态都成立。
        """
        # 本实现只建倒排索引（检索）：调用方要求只动正排时整体跳过。
        if mode is IndexWriteMode.FORWARD_ONLY:
            return
        logger.info("FulltextIndexBuilder: updating index for %d units", len(units))
        for unit in units:
            if self._store is not None:
                self._store.delete(unit.scope, [unit.id])
                self._store.insert(unit.scope, [content_document(unit)])
            # L0/L1：先删旧 record（store 非空才删），再按新 layers 重建——避免旧分层残留
            delete_layer_documents(self._layer_ports(), unit.id, unit.scope)
            self._build_layers(unit)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        # 本实现只持有检索索引：SOFT/HARD 都要移出检索，行为相同。
        logger.info("FulltextIndexBuilder: removing %d units from index", len(units))
        for unit in units:
            if self._store is not None:
                self._store.delete(unit.scope, [unit.id])
            delete_layer_documents(self._layer_ports(), unit.id, unit.scope)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # L0/L1 分层索引辅助
    # ------------------------------------------------------------------

    def _build_layers(self, unit: MemoryUnit) -> None:
        """对该 unit 构建 L0/L1 全文索引（store 非空且 layers 非空才写独立 store）。"""
        build_fulltext_layers(self._layer_ports(), unit)


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("fulltext")
def _build(config):
    return FulltextIndexBuilder(
        StorageProducer.resolve(config),
        layers_enabled=config.get("layers_index_enabled", True),
    )
