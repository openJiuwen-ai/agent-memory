"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 写入注入的 :class:`~storage.fulltext.FulltextStore`
（hot 轻量索引）。自留 ``id→scope`` 映射，使无 scope 入参的 ``remove`` 也能
定位到对应 scope 删除索引。
"""

from __future__ import annotations

from typing import Dict, List

from common.log import get_logger
from common.type_def import T_INVALID_OPEN, MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.fulltext import FulltextProducer, FulltextStore
from storage.types import Document

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
            "source": unit.source.value,
            "content_layer": layer,  # l2=content 全文；l0/l1 为分层文档（见 F01）
        }
    )
    temporal = unit.temporal
    for field, value in (("t_event", temporal.t_event), ("t_valid", temporal.t_valid)):
        if value is not None:
            metadata[field] = int(value.timestamp() * 1000)
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
        store: FulltextStore,
        fulltext_l0: FulltextStore | None = None,
        fulltext_l1: FulltextStore | None = None,
    ) -> None:
        self._store = store
        # L0/L1 分层 store：None 表示不构建该层索引（向后兼容 + 配置降级）。
        self._fulltext_l0 = fulltext_l0
        self._fulltext_l1 = fulltext_l1
        self._scope_of: Dict[str, Scope] = {}

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

    def build(self, units: List[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: building index for %d units", len(units))
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            doc = self._doc(unit)
            logger.info("FulltextIndexBuilder: indexing unit id=%s tier=%s tags=%s content=%s",
                         unit.id[:8], unit.tier.value, unit.tags, unit.content[:200])
            self._store.insert(unit.scope, [doc])
            # L0/L1 分层：store 非空且 layers 非空才写独立 store（分表）
            self._build_layers(unit)

    def update(self, units: List[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: updating index for %d units", len(units))
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            self._store.update(unit.scope, [self._doc(unit)])
            # L0/L1：先删旧 record（store 非空才删），再按新 layers 重建——避免旧分层残留
            self._delete_layer_records(unit.id, unit.scope)
            self._build_layers(unit)

    def remove(self, unit_ids: List[str]) -> None:
        logger.info("FulltextIndexBuilder: removing %d unit ids from index", len(unit_ids))
        for unit_id in unit_ids:
            scope = self._scope_of.pop(unit_id, None)
            if scope is not None:
                self._store.delete(scope, [unit_id])
                # 删 L0/L1 分层 record（store 非空才删，幂等）
                self._delete_layer_records(unit_id, scope)

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
    # L0/L1 分层索引：layers_index_enabled（默认 true）开时，取 fulltext_store 的
    # layers_l0/l1 具名实例（指向不同 index = 分表）注入；未配置则该层传 None，
    # builder 跳过该层（向后兼容 + 配置降级）。与 HybridIndexBuilder._build 一致。
    layers_enabled = config.get("layers_index_enabled", True)

    def _opt_dep(producer_cls, name):
        """取具名实例；未配置则返回 None（不报错，builder 跳过该层）。

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

    return FulltextIndexBuilder(
        FulltextProducer.dep(config, default="memory"),
        fulltext_l0=_opt_dep(FulltextProducer, "layers_l0"),
        fulltext_l1=_opt_dep(FulltextProducer, "layers_l1"),
    )
