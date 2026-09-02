"""文档记忆的 :class:`~construction.index_builder.IndexBuilder` 实现。

文档模式（``globals.write_document=true``）下作为 ``IndexBuilder`` 的装配方：
真源是 ``md`` 文件 + ``shadow`` 影子索引，不再写 ``KV``。本实现是「全委托
``Storage``」的薄编排层——``build``/``update``/``remove`` 各自把 ``units`` 与
``mode`` 原样下传给 ``storage.add``/``update``/``delete``，由 :class:`CompositeStorage`
内部按 ``should_write_document`` 分流到 ``md.write`` + ``shadow.insert_units``。

为什么是 IndexBuilder 的一种实现而非另开 engine 路径：
:func:`~control.engine_impl.in_memory_engine.InMemoryEngine.write` 默认路径只调
``index_builder.build``（update/delete 同调 ``self._index``），契约要求「记忆写入只经
本算子」——文档模式下也必须经由 IndexBuilder 接住这次调用。把真源落盘收进
``storage.add`` 的文档分流、再由本算子委托，既不破 engine 契约，也不让
``md_filename`` 回填时序泄漏到 construction 层（``md.write`` 与
``shadow.insert_units`` 闭环在 ``storage.add`` 同一调用栈内，见
``CompositeStorage.add`` 文档分支）。

``IndexWriteMode`` / ``IndexRemoveMode`` 原样下传（与契约「不支持细粒度控制的实现
把枚举原样下传」一致）：

- ``RETRIEVAL_ONLY``：文档场景影子索引即真源、无独立检索索引可补建，本算子整段
  跳过（连 ``storage.add`` 都不调），语义同 ``CompositeStorage.add`` 的
  ``RETRIEVAL_ONLY`` 早退。
- ``FORWARD_ONLY``（update）：文档真源是 ``md``，``storage.update`` 文档分流走
  ``shadow.update_units`` 回写本体（``md`` 块内容以 ``content_hash`` 判定是否重写），
  无检索索引需另动。
- ``SOFT``（remove）：``storage.delete`` 文档分流保留 ``md`` 本体、只让影子索引
  退出检索——与 ``KV`` 场景 ``SOFT`` 保留本体的语义对齐。

端口取法：``md`` 与 ``shadow`` 一律从注入的 ``storage`` 取端口
（``storage.markdown_port()`` / ``storage.shadow_index_port()``），**不自行 new**，
保证读写同源——与 ``ForwardIndexBuilder`` 取 ``storage.kv`` 同构。
"""

from __future__ import annotations

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.storage import Storage, StorageProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)


class DocumentIndexBuilder(IndexBuilder):
    """文档记忆的薄编排层——全委托注入的 ``Storage``。

    构造即解析：``storage`` 非文档模式（``should_write_document()`` 为 ``False``）或
    缺 ``markdown`` / ``shadow`` 端口时直接抛，不拖到首次写入才以
    ``AttributeError`` 形式暴露——与 ``ForwardIndexBuilder`` 构造期解析 KV 端口同范式。
    """

    def __init__(self, storage: Storage) -> None:
        if not storage.should_write_document():
            raise UnsupportedStorageCapabilityError(
                "DocumentIndexBuilder 要求文档模式（globals.write_document=true），"
                "但注入的 Storage 非文档模式"
            )
        if not storage.has_markdown_port() or not storage.has_shadow_port():
            raise UnsupportedStorageCapabilityError(
                "DocumentIndexBuilder 要求 markdown + shadow 端口均就绪"
            )
        self._storage = storage

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("DocumentIndexBuilder: building document memory for %d units", len(units))
        for unit in units:
            self._storage.add(unit.scope, [unit], mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("DocumentIndexBuilder: updating document memory for %d units", len(units))
        for unit in units:
            self._storage.update(unit.scope, [unit], mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        logger.info("DocumentIndexBuilder: removing %d units (mode=%s)", len(units), mode)
        for unit in units:
            self._storage.delete(unit.scope, [unit.id], mode=mode)

    def rebuild(self) -> None:
        # 文档真源（md 文件）与影子索引同生命周期，无独立重建路径——与 HybridIndexBuilder 一致。
        return None


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("document")
def _build(config):
    return DocumentIndexBuilder(StorageProducer.resolve(config))
