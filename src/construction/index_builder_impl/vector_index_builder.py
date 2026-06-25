"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 经注入的 :class:`~common.chunker.base.Chunker` 切片
→ :class:`~common.embedder.base.Embedder` 向量化 → 写入
:class:`~storage.vector.VectorStore`（向量 ANN 索引）。
同时在 :class:`~storage.kv.KVStore` 维护 chunk_id 跟踪记录（update/remove 时
读取旧 chunk_id 列表）。自留 ``id→scope`` 映射，使无 scope 入参的 ``remove``
也能定位到对应 scope 删除索引。

VectorRecord.id 采用 ``{unit.id}-{chunk.id}`` 拼接格式，确保全局唯一。
"""

from __future__ import annotations

import json
from typing import Dict, List

from common.chunker.base import Chunker, ChunkerProducer
from common.embedder.base import Embedder, EmbedderProducer
from common.log import get_logger
from common.type_def import MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.kv import KvProducer, KVStore
from storage.types import VectorRecord
from storage.vector import VectorProducer, VectorStore

logger = get_logger(__name__)


class VectorIndexBuilder(IndexBuilder):
    """向量索引构建：MemoryUnit → Chunker → Embedder → VectorStore + chunk tracking。"""

    def __init__(
        self,
        vector_store: VectorStore,
        kv_store: KVStore,
        chunker: Chunker,
        embedder: Embedder,
    ) -> None:
        self._vector_store = vector_store
        self._kv_store = kv_store
        self._chunker = chunker
        self._embedder = embedder
        self._scope_of: Dict[str, Scope] = {}

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    @staticmethod
    def _chunk_tracking_key(unit_id: str) -> str:
        """unit_id → KVStore 中 chunk_id 跟踪记录的 key。"""
        return f"/index/chunks/{unit_id}"

    # ------------------------------------------------------------------
    # IndexBuilder 契约
    # ------------------------------------------------------------------

    def build(self, units: List[MemoryUnit]) -> None:
        """为一批记忆单元构建向量索引。"""
        logger.info("VectorIndexBuilder: building index for %d units", len(units))
        all_records: list[VectorRecord] = []
        chunk_tracking: dict[str, list[str]] = {}

        for unit in units:
            self._scope_of[unit.id] = unit.scope
            logger.info("VectorIndexBuilder: indexing unit id=%s tier=%s provenance=%s content=%s",
                         unit.id[:8], unit.tier.value, unit.provenance, unit.content[:200])
            chunks = self._chunker.chunk(
                text=unit.content,
                unit_id=unit.id,
                metadata={"tier": unit.tier.value},
            )
            if not chunks:
                continue

            # 向量化
            texts = [c.text for c in chunks]
            try:
                vectors = self._embedder.embed(texts)
            except Exception as exc:
                logger.warning("VectorIndexBuilder: Embedder.embed failed for unit %s: %s", unit.id[:8], exc)
                continue

            # 构建 VectorRecord（id = unit.id + "-" + chunk.id，确保全局唯一）
            chunk_ids: list[str] = []
            for chunk, vector in zip(chunks, vectors):
                record_id = f"{unit.id}-{chunk.id}"
                record = VectorRecord(
                    id=record_id,
                    vector=vector,
                    metadata={
                        "unit_id": unit.id,
                        "tier": unit.tier.value,
                        "lifecycle": unit.lifecycle.value,  # 召回下推 lifecycle 谓词需此字段（真后端按缺失字段排他）
                        "seq": str(chunk.seq),
                    },
                )
                all_records.append(record)
                chunk_ids.append(record_id)

            chunk_tracking[unit.id] = chunk_ids

        if not all_records:
            return

        # 写入 VectorStore（按 scope 分组）
        scope_groups: dict[tuple, list[VectorRecord]] = {}
        unit_scope_map: dict[str, tuple] = {}
        for unit in units:
            key = (unit.scope.org, unit.scope.user, unit.scope.agent, unit.scope.session)
            unit_scope_map[unit.id] = key

        for record in all_records:
            unit_id = record.metadata.get("unit_id", "")
            key = unit_scope_map.get(unit_id, ("", "", "", ""))
            scope_groups.setdefault(key, []).append(record)

        for key, group_records in scope_groups.items():
            scope = Scope(org=key[0], user=key[1], agent=key[2], session=key[3])
            try:
                self._vector_store.insert(scope, group_records)
            except Exception as exc:
                logger.warning("VectorIndexBuilder: VectorStore.insert failed for scope %s: %s", key, exc)
                try:
                    self._vector_store.update(scope, group_records)
                except Exception as exc2:
                    logger.error("VectorIndexBuilder: VectorStore.update also failed for scope %s: %s", key, exc2)

        # chunk_id 跟踪写入 KVStore
        for unit_id, chunk_ids in chunk_tracking.items():
            key_tuple = unit_scope_map.get(unit_id, ("", "", "", ""))
            scope = Scope(org=key_tuple[0], user=key_tuple[1], agent=key_tuple[2], session=key_tuple[3])
            kv_key = self._chunk_tracking_key(unit_id)
            try:
                if self._kv_store.exists(scope, kv_key):
                    self._kv_store.update(scope, kv_key, json.dumps(chunk_ids).encode())
                else:
                    self._kv_store.insert(scope, kv_key, json.dumps(chunk_ids).encode())
            except Exception as exc:
                logger.warning("VectorIndexBuilder: KVStore chunk tracking write failed for %s: %s", kv_key, exc)

    def update(self, units: List[MemoryUnit]) -> None:
        """增量更新向量索引：先删旧 chunk → 再建新。"""
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            kv_key = self._chunk_tracking_key(unit.id)
            try:
                raw = self._kv_store.get(unit.scope, kv_key)
                old_chunk_ids = json.loads(raw.decode())
                self._vector_store.delete(unit.scope, old_chunk_ids)
            except Exception as exc:
                logger.warning("VectorIndexBuilder: no old chunks found for unit %s: %s", unit.id[:8], exc)

        self.build(units)

    def remove(self, unit_ids: List[str]) -> None:
        """删除一批记忆单元对应的向量索引条目（幂等）。"""
        for unit_id in unit_ids:
            scope = self._scope_of.pop(unit_id, None)
            if scope is None:
                continue
            self._remove_by_scope(unit_id, scope)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def remove_with_scope(self, unit_ids: List[str], scope: Scope) -> None:
        """已知 scope 时直接删除索引条目，避免 lookup。"""
        for unit_id in unit_ids:
            self._remove_by_scope(unit_id, scope)

    def _remove_by_scope(self, unit_id: str, scope: Scope) -> None:
        """删除单个 unit 的向量索引条目 + chunk tracking。"""
        kv_key = self._chunk_tracking_key(unit_id)
        try:
            raw = self._kv_store.get(scope, kv_key)
            chunk_ids = json.loads(raw.decode())
            self._vector_store.delete(scope, chunk_ids)
        except Exception:
            logger.warning("VectorIndexBuilder: no chunk tracking found for %s", unit_id)

        # 清理 chunk tracking KV 记录
        try:
            self._kv_store.delete(scope, kv_key)
        except Exception:
            pass  # 幂等


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("vector")
def _build(config):
    # 向量/真源 Store 与召回侧共享同一实例；embedder/chunker 与查询、构建侧共享。
    return VectorIndexBuilder(
        vector_store=VectorProducer.dep(config, default="memory"),
        kv_store=KvProducer.dep(config, default="memory"),
        chunker=ChunkerProducer.dep(config, default="fixed_window"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
    )
