"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 写入注入的 :class:`~storage.fulltext.FulltextStore`
（hot 轻量索引）。自留 ``id→scope`` 映射，使无 scope 入参的 ``remove`` 也能
定位到对应 scope 删除索引。
"""

from __future__ import annotations

import json
from typing import Dict, List

from common.log import get_logger
from common.type_def import MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.fulltext import FulltextProducer, FulltextStore
from storage.types import Document

logger = get_logger(__name__)


class FulltextIndexBuilder(IndexBuilder):
    """把记忆单元 content 写入全文索引（hot 轻量索引）。"""

    def __init__(self, store: FulltextStore) -> None:
        self._store = store
        self._scope_of: Dict[str, Scope] = {}

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def _doc(self, unit: MemoryUnit) -> Document:
        return Document(
            id=unit.id,
            text=unit.content,
            metadata={
                "unit_id": unit.id,
                "tier": unit.tier.value,
                "lifecycle": unit.lifecycle.value,  # 召回下推 lifecycle 谓词需此字段（真后端按缺失字段排他）
                "tags": json.dumps(unit.tags),
                "source": unit.source.value,
            },
        )

    def build(self, units: List[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: building index for %d units", len(units))
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            doc = self._doc(unit)
            logger.info("FulltextIndexBuilder: indexing unit id=%s tier=%s tags=%s content=%s",
                         unit.id[:8], unit.tier.value, unit.tags, unit.content[:200])
            self._store.insert(unit.scope, [doc])

    def update(self, units: List[MemoryUnit]) -> None:
        logger.info("FulltextIndexBuilder: updating index for %d units", len(units))
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            self._store.update(unit.scope, [self._doc(unit)])

    def remove(self, unit_ids: List[str]) -> None:
        logger.info("FulltextIndexBuilder: removing %d unit ids from index", len(unit_ids))
        for unit_id in unit_ids:
            scope = self._scope_of.pop(unit_id, None)
            if scope is not None:
                self._store.delete(scope, [unit_id])

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #



@IndexBuilderProducer.register("fulltext")
def _build(config):
    return FulltextIndexBuilder(FulltextProducer.dep(config, default="memory"))
