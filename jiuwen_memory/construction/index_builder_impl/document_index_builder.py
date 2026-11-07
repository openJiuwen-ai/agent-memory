"""文档记忆的 :class:`~construction.index_builder.IndexBuilder` 实现。

文档模式（``globals.write_document=true``）下作为 ``IndexBuilder`` 的装配方：
真源是 ``md`` 文件 + ``shadow`` 影子索引，不再写 ``KV``。本实现是「全委托
``DomainStore``」的薄编排层——``build``/``update``/``remove`` 各自把 ``units`` 与
``mode`` 原样下传给 ``domain_store.add``/``update``/``delete``，由
:class:`CompositeDomainStore` 内部按 ``should_write_document`` 分流到
``md.write`` + ``shadow.insert_units``。

为什么是 IndexBuilder 的一种实现而非另开 engine 路径：
:func:`~control.engine_impl.in_memory_engine.InMemoryEngine.write` 默认路径只调
``index_builder.build``（update/delete 同调 ``self._index``），契约要求「记忆写入只经
本算子」——文档模式下也必须经由 IndexBuilder 接住这次调用。把真源落盘收进
``domain_store.add`` 的文档分流、再由本算子委托，既不破 engine 契约，也不让
``md_filename`` 回填时序泄漏到 construction 层（``md.write`` 与
``shadow.insert_units`` 闭环在 ``domain_store.add`` 同一调用栈内，见
``CompositeDomainStore.add`` 文档分支）。

``IndexWriteMode`` / ``IndexRemoveMode`` 原样下传（与契约「不支持细粒度控制的实现
把枚举原样下传」一致）：

- ``RETRIEVAL_ONLY``：文档场景影子索引即真源、无独立检索索引可补建，本算子整段
  跳过（连 ``domain_store.add`` 都不调），语义同 ``CompositeDomainStore.add`` 的
  ``RETRIEVAL_ONLY`` 早退。
- ``FORWARD_ONLY``（update）：文档真源是 ``md``，``domain_store.update`` 文档分流走
  ``shadow.update_units`` 回写本体（``md`` 块内容以 ``content_hash`` 判定是否重写），
  无检索索引需另动。
- ``SOFT``（remove）：文档模式下为 no-op——检索退出不靠删投影，由调用方先
  ``lifecycle.transition`` 改状态（``update(FORWARD_ONLY)`` 同步影子索引
  lifecycle 投影列），检索侧靠谓词下推 + retriever 复核排除。调用方契约：
  **先 transition 再 remove(SOFT)**，裸调 SOFT 不会使 unit 退出检索。

端口取法：``md`` 与 ``shadow`` 一律由 :class:`CompositeDomainStore` 内部经注入的
``StoreManager`` 取端口（``manager.markdown()`` / ``manager.shadow_index()``），
**不自行 new**，保证读写同源——与 :class:`ForwardIndexBuilder` 取
``storage.kv`` 同构。本算子只持有 ``DomainStore``，不做端口解析。
"""

from __future__ import annotations

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)


class DocumentIndexBuilder(IndexBuilder):
    """文档记忆的薄编排层——全委托注入的 ``DomainStore``。

    构造即解析：``domain_store`` 未开启文档模式（无 ``should_write_document`` 或返回
    ``False``）时直接抛，不拖到首次写入才以 ``AttributeError`` 形式暴露——与
    :class:`ForwardIndexBuilder` 构造期解析 KV 端口同范式。端口就绪性（md/shadow）由
    :class:`CompositeDomainStore` 内部经注入的 ``StoreManager`` 保证，非本算子职责。
    """

    def __init__(self, domain_store: DomainStore) -> None:
        should_write = getattr(domain_store, "should_write_document", None)
        if should_write is None or not should_write():
            raise UnsupportedStorageCapabilityError(
                "DocumentIndexBuilder 要求文档模式（globals.write_document=true），"
                "但注入的 DomainStore 非文档模式"
            )
        self._domain = domain_store

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("DocumentIndexBuilder: building document memory for %d units", len(units))
        for unit in units:
            self._domain.add(unit.scope, [unit], mode=mode)

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("DocumentIndexBuilder: updating document memory for %d units", len(units))
        for unit in units:
            self._domain.update(unit.scope, [unit], mode=mode)

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        logger.info("DocumentIndexBuilder: removing %d units (mode=%s)", len(units), mode)
        for unit in units:
            self._domain.delete(unit.scope, [unit.id], mode=mode)

    def rebuild(self) -> None:
        # 文档真源（md 文件）与影子索引同生命周期，无独立重建路径——与 HybridIndexBuilder 一致。
        return None


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("document")
def _build(config):
    return DocumentIndexBuilder(
        StoreManagerProducer.resolve(config).domain_store(
            resolve_name(config, "domain_store")
        )
    )
