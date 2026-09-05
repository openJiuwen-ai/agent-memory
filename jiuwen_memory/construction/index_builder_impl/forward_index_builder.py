"""正排索引的 :class:`~construction.index_builder.IndexBuilder` 实现。

正排即记忆本体：倒排/向量/实体等派生索引召回出 ``unit_id`` 后，回正排取完整
``MemoryUnit``。本实现把 ``MemoryUnit`` 投影成 KV 记录（``memory_key`` 定 key、
``memory_codec`` 定字节）写入注入的 KV 端口，与 ``FulltextIndexBuilder`` 写
FulltextStore、``VectorIndexBuilder`` 写 VectorStore 同构——一个子 builder 只负责
一种索引形式，端口一律从 ``Storage`` 取，从而与读侧共用同一批 Store 实例。

正排是唯一需要**两向投影**的索引形式：写侧 ``unit → bytes`` 在本实现，读侧
``bytes → unit`` 在 ``Storage.get``/``list``。两半靠 ``memory_key`` 与
``memory_codec`` 这对跨层共享契约对齐（``KVStore.list`` 本就按 ``MEMORY_KEY_PREFIX``
扫描，该 key 方案早已是跨层约定）。
"""

from __future__ import annotations

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import MemoryUnit, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder, IndexBuilderProducer
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import (
    StoreManager,
    StoreManagerProducer,
    resolve_name,
)
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)


class ForwardIndexBuilder(IndexBuilder):
    """把 MemoryUnit 落成正排 KV 记录。

    ``storage.kv()`` 在装配期即解析：StorageManager 没有 KV 能力时立刻抛
    :class:`~common.errors.UnsupportedStorageCapabilityError`，而不是拖到首次写入
    才暴露——正排是真源，缺了它整条读路径都无从谈起。
    """

    def __init__(self, storage: StoreManager, *, kv_name: str = "default") -> None:
        # 构造即解析：manager 无该命名端口时此处直接抛
        # UnsupportedStorageCapabilityError，不拖到首次写入才以 AttributeError 暴露。
        self._kv = storage.kv(kv_name)

    @property
    def kv_store(self) -> KVStore:
        """正排 store（只读；供测试断言与装配期校验）。"""
        return self._kv

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        # 本实现只负责正排：RETRIEVAL_ONLY 即整体跳过。
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("ForwardIndexBuilder: building forward index for %d units", len(units))
        for unit in units:
            self._kv.insert(unit.scope, memory_key(unit.id), dumps(unit))

    def update(
        self, units: list[MemoryUnit], *, mode: IndexWriteMode = IndexWriteMode.ALL
    ) -> None:
        # 本实现即正排本身：RETRIEVAL_ONLY 即整体跳过，其余取值行为相同。
        if mode is IndexWriteMode.RETRIEVAL_ONLY:
            return
        logger.info("ForwardIndexBuilder: updating forward index for %d units", len(units))
        for unit in units:
            self._kv.update(unit.scope, memory_key(unit.id), dumps(unit))

    def remove(
        self, units: list[MemoryUnit], *, mode: IndexRemoveMode = IndexRemoveMode.HARD
    ) -> None:
        # 软删除保留记忆本体，正排无事可做。
        if mode is IndexRemoveMode.SOFT:
            return
        logger.info("ForwardIndexBuilder: removing %d units from forward index", len(units))
        for unit in units:
            self._kv.delete(unit.scope, memory_key(unit.id))

    def rebuild(self) -> None:
        # 正排即真源本身，没有可从别处重建的派生物。
        return None


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("forward")
def _build(config):
    return ForwardIndexBuilder(
        StoreManagerProducer.resolve(config),
        kv_name=resolve_name(config, "kv_store"),
    )
