"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 写入注入的 :class:`~storage.fulltext.FulltextStore`
（hot 轻量索引）。删除入口接收 ``MemoryUnit``，直接使用其 scope 定位索引。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import T_EVENT_UNKNOWN, T_INVALID_OPEN, MemoryUnit, Scope
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import Document

logger = get_logger(__name__)


def _index_metadata(unit: MemoryUnit, *, layer: str) -> dict[str, object]:
    """构造可过滤索引投影；用户 metadata 原样带入，系统真源字段随后覆盖。

    ``metadata`` 值为 JSON 标量原生类型，后端据此建 double/boolean/keyword mapping
    并在 top-k 截断前原生下推。UnitReader 复核读的是同一个对象，两侧判定不分叉。
    """
    metadata = dict(unit.metadata)
    metadata.update(
        {
            "unit_id": unit.id,
            "tier": unit.tier.value,
            # 召回下推 lifecycle 谓词需此字段（真后端按缺失字段排他）。
            "lifecycle": unit.lifecycle.value,
            "tags": list(unit.tags),  # 真数组，后端才能按成员做 term 匹配
            "entities": list(unit.entities),  # 实体明文列表；L2 召回读出后做 hash 反查关联记忆
            "source": unit.source.value,
            "content_layer": layer,  # l2=content 全文；l0/l1 为分层文档（见 F01）
        }
    )
    temporal = unit.temporal
    # t_event 恒写：空（未知事件时间，F07 派生常为此值）落哨兵，否则该字段缺失会被
    # 事件窗下推按缺失字段排他——含时间词 query 对这批 unit 系统性空召回。
    # 哨兵与谓词、memory_filter 三处同改（见 common.type_def.T_EVENT_UNKNOWN）。
    metadata["t_event"] = (
        int(temporal.t_event.timestamp() * 1000)
        if temporal.t_event is not None
        else T_EVENT_UNKNOWN
    )
    # t_valid 仍有值才写：未生效记忆本就稀疏，下推用 LTE as_of 即可，缺值放行不破。
    if temporal.t_valid is not None:
        metadata["t_valid"] = int(temporal.t_valid.timestamp() * 1000)
    # t_invalid 恒写：空（永久有效）落哨兵值，否则该字段缺失会被 `t_invalid > as_of`
    # 的下推按缺失字段排他——那批正是回溯查询最该命中的活跃记忆。
    metadata["t_invalid"] = (
        int(temporal.t_invalid.timestamp() * 1000)
        if temporal.t_invalid is not None
        else T_INVALID_OPEN
    )
    return metadata


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
        self._fulltext_l0 = _fulltext_port(storage, "layers_l0", layers_enabled)
        self._fulltext_l1 = _fulltext_port(storage, "layers_l1", layers_enabled)

    @property
    def fulltext_l0(self) -> FulltextStore | None:
        """L0 分层 store（只读；None 表示该层未注入，构建跳过）。"""
        return self._fulltext_l0

    @property
    def fulltext_l1(self) -> FulltextStore | None:
        """L1 分层 store（只读；None 表示该层未注入）。"""
        return self._fulltext_l1

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def _doc(self, unit: MemoryUnit) -> Document:
        return Document(
            id=unit.id,  # L2 文档沿用 unit.id（F01 允许短期保留旧 id 兼容删除/update）
            text=unit.content,
            metadata=_index_metadata(unit, layer="l2"),
        )

    def _layer_doc(self, unit: MemoryUnit, layer: str) -> Document:
        """L0/L1 分层文档：id={unit_id}:l0/:l1（对齐 F01 命名），text=layers 对应字段。"""
        text = unit.layers.l0 if layer == "l0" else unit.layers.l1
        return Document(
            id=f"{unit.id}:{layer}",
            text=text,
            metadata=_index_metadata(unit, layer=layer),
        )

    def build(self, units: list[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: building index for %d units", len(units))
        for unit in units:
            if self._store is not None:
                doc = self._doc(unit)
                logger.info(
                    "FulltextIndexBuilder: indexing unit id=%s tier=%s tags=%s content=%s",
                    unit.id[:8],
                    unit.tier.value,
                    unit.tags,
                    unit.content[:200],
                )
                self._store.insert(unit.scope, [doc])
            # L0/L1 分层：store 非空且 layers 非空才写独立 store（分表）
            self._build_layers(unit)

    def update(self, units: list[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: updating index for %d units", len(units))
        for unit in units:
            if self._store is not None:
                self._store.update(unit.scope, [self._doc(unit)])
            # L0/L1：先删旧 record（store 非空才删），再按新 layers 重建——避免旧分层残留
            self._delete_layer_records(unit.id, unit.scope)
            self._build_layers(unit)

    def remove(self, units: list[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: removing %d units from index", len(units))
        for unit in units:
            if self._store is not None:
                self._store.delete(unit.scope, [unit.id])
            self._delete_layer_records(unit.id, unit.scope)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # L0/L1 分层索引辅助
    # ------------------------------------------------------------------

    def _build_layers(self, unit: MemoryUnit) -> None:
        """对该 unit 构建 L0/L1 全文索引（store 非空且 layers 非空才写独立 store）。"""
        for store, layer in ((self._fulltext_l0, "l0"), (self._fulltext_l1, "l1")):
            if store is None:
                continue  # 该层未注入，跳过
            text = unit.layers.l0 if layer == "l0" else unit.layers.l1
            if not (text or "").strip():
                continue  # 该 unit 无此层分层，跳过
            doc = self._layer_doc(unit, layer)
            try:
                store.insert(unit.scope, [doc])
            except Exception as exc:
                logger.warning(
                    "FulltextIndexBuilder: layers %s insert failed for %s: %s, try update",
                    layer, unit.id[:8], exc,
                )
                try:
                    store.update(unit.scope, [doc])
                except Exception as exc2:
                    logger.error(
                        "FulltextIndexBuilder: layers %s update also failed for %s: %s",
                        layer, unit.id[:8], exc2,
                    )

    def _delete_layer_records(self, unit_id: str, scope: Scope) -> None:
        """删除该 unit 的 L0/L1 分层文档（幂等）。store 非空才删对应层。"""
        for store, layer in ((self._fulltext_l0, "l0"), (self._fulltext_l1, "l1")):
            if store is None:
                continue
            try:
                store.delete(scope, [f"{unit_id}:{layer}"])
            except Exception as exc:
                logger.warning(
                    "FulltextIndexBuilder: delete layer %s doc failed for %s: %s",
                    layer, unit_id[:8], exc,
                )


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@IndexBuilderProducer.register("fulltext")
def _build(config):
    return FulltextIndexBuilder(
        StorageProducer.resolve(config),
        layers_enabled=config.get("layers_index_enabled", True),
    )


def _fulltext_port(storage: Storage, name: str, enabled: bool) -> FulltextStore | None:
    if not enabled or not storage.has_fulltext_port(name):
        return None
    return storage.fulltext_port(name)
