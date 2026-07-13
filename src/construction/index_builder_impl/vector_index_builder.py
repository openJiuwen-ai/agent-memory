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
    """向量索引构建：MemoryUnit → Chunker → Embedder → VectorStore + chunk tracking。

    L0/L1 分层索引（架构 §9.1）：``unit.layers.l0``/``.l1`` 非空且对应 store 已注入时，
    对整段 l0/l1 文本（不切片）做 embed，写独立 VectorStore 实例（不同 collection = 分表），
    record id = ``{unit_id}-l0``/``{unit_id}-l1``。store 为 None 时跳过该层，不影响 content。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        kv_store: KVStore,
        chunker: Chunker,
        embedder: Embedder,
        vector_l0: VectorStore | None = None,
        vector_l1: VectorStore | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._kv_store = kv_store
        self._chunker = chunker
        self._embedder = embedder
        # L0/L1 分层 store：None 表示不构建该层索引（向后兼容 + 配置降级）。
        self._vector_l0 = vector_l0
        self._vector_l1 = vector_l1
        self._scope_of: Dict[str, Scope] = {}

    @property
    def vector_l0(self) -> VectorStore | None:
        """L0 分层 store（只读；None 表示该层未注入，recall/构建跳过）。"""
        return self._vector_l0

    @property
    def vector_l1(self) -> VectorStore | None:
        """L1 分层 store（只读；None 表示该层未注入）。"""
        return self._vector_l1

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
                        "content_layer": "l2",  # L2=content 全文 chunk（与 L0/L1 分层 record 对齐，见 F01）
                    },
                )
                all_records.append(record)
                chunk_ids.append(record_id)

            chunk_tracking[unit.id] = chunk_ids

        # L0/L1 分层索引：store 非空且 layers 非空才整段 embed 写独立 store（分表）。
        # 必须在下方 ``if not all_records: return`` 之前执行——content 切不出 chunk 时
        # all_records 为空，但 unit 的 layers.l0/l1 仍可能非空、仍需建分层索引（update
        # 路径亦依赖此处按新 layers 重建，见 update 的删旧→重建约定）。
        self._build_layers(units)

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
        """增量更新向量索引：先删旧 chunk + 旧 L0/L1 record → 再建新。

        SUPERSEDE/UPDATE 场景下 unit 可能从「有 layers」变「无 layers」（或反之），故 L0/L1
        必须先删旧 record（store 非空才删），再由 build 按新 layers 决定是否重建——否则
        旧 L0/L1 残留。
        """
        for unit in units:
            self._scope_of[unit.id] = unit.scope
            kv_key = self._chunk_tracking_key(unit.id)
            try:
                raw = self._kv_store.get(unit.scope, kv_key)
                old_chunk_ids = json.loads(raw.decode())
                self._vector_store.delete(unit.scope, old_chunk_ids)
            except Exception as exc:
                logger.warning("VectorIndexBuilder: no old chunks found for unit %s: %s", unit.id[:8], exc)
            # 先删旧 L0/L1 record（幂等），build 会按新 layers 重建
            self._delete_layer_records(unit.id, unit.scope)

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
        """删除单个 unit 的向量索引条目 + chunk tracking + L0/L1 record。"""
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

        # 删 L0/L1 分层 record（store 非空才删，幂等）
        self._delete_layer_records(unit_id, scope)

    # ------------------------------------------------------------------
    # L0/L1 分层索引辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _layer_record_id(unit_id: str, layer: str) -> str:
        """分层 record id：``{unit_id}-layer-l0`` / ``{unit_id}-layer-l1``（对齐 F01 命名），
        与 content 的 chunk id（``{unit_id}-{chunk_id}``）不冲突。"""
        return f"{unit_id}-layer-{layer}"

    def _build_layers(self, units: List[MemoryUnit]) -> None:
        """对带 layers 的 unit 构建 L0/L1 向量索引（整段 embed，写独立 store）。

        双重判定：store 非空（注入了该层）且 layers 字段非空（该 unit 有分层）才执行，
        任一为空则跳过该层该 unit——不报错、不建空记录。
        """
        # (store, layer, getter) 三元组：layer 标识 + 取 unit.layers 对应字段
        layer_specs = [
            (self._vector_l0, "l0", lambda u: u.layers.l0),
            (self._vector_l1, "l1", lambda u: u.layers.l1),
        ]
        for store, layer, get_text in layer_specs:
            if store is None:
                continue  # 该层未注入，跳过
            self._build_one_layer(store, layer, get_text, units)

    def _build_one_layer(self, store: VectorStore, layer: str, get_text, units: List[MemoryUnit]) -> None:
        """构建单层（L0 或 L1）向量索引：整段 embed → 写独立 store。

        L0/L1 是 unit 级整体（不切片），一条 unit 在该层表最多一条 record。
        store.insert 与 content 一样走「insert 失败→update 兜底」。
        """
        pending: list[tuple[MemoryUnit, str]] = []  # (unit, text) 待 embed
        for unit in units:
            text = (get_text(unit) or "").strip()
            if not text:
                continue
            pending.append((unit, text))

        if not pending:
            return

        # 批量 embed 整段文本
        try:
            vectors = self._embedder.embed([t for _, t in pending])
        except Exception as exc:
            logger.warning("VectorIndexBuilder: layers %s embed failed: %s", layer, exc)
            return

        # 按 scope 分组写入对应 store
        groups: dict[tuple, list[VectorRecord]] = {}
        for (unit, text), vector in zip(pending, vectors):
            record = VectorRecord(
                id=self._layer_record_id(unit.id, layer),
                vector=vector,
                metadata={
                    "unit_id": unit.id,
                    "tier": unit.tier.value,
                    "lifecycle": unit.lifecycle.value,
                    "content_layer": layer,  # "l0" | "l1"（L2 在 content 表 chunk record，见 F01）
                },
            )
            key = (unit.scope.org, unit.scope.user, unit.scope.agent, unit.scope.session)
            groups.setdefault(key, []).append(record)

        for key, records in groups.items():
            scope = Scope(org=key[0], user=key[1], agent=key[2], session=key[3])
            try:
                store.insert(scope, records)
            except Exception as exc:
                logger.warning(
                    "VectorIndexBuilder: layers %s insert failed for scope %s: %s, try update",
                    layer, key, exc,
                )
                try:
                    store.update(scope, records)
                except Exception as exc2:
                    logger.error(
                        "VectorIndexBuilder: layers %s update also failed for scope %s: %s",
                        layer, key, exc2,
                    )

    def _delete_layer_records(self, unit_id: str, scope: Scope) -> None:
        """删除该 unit 的 L0/L1 分层 record（幂等）。store 非空才删对应层。"""
        for store, layer in ((self._vector_l0, "l0"), (self._vector_l1, "l1")):
            if store is None:
                continue
            try:
                store.delete(scope, [self._layer_record_id(unit_id, layer)])
            except Exception as exc:
                logger.warning(
                    "VectorIndexBuilder: delete layer %s record failed for %s: %s",
                    layer, unit_id[:8], exc,
                )


# -- 注册到 IndexBuilderProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@IndexBuilderProducer.register("vector")
def _build(config):
    # 向量/真源 Store 与召回侧共享同一实例；embedder/chunker 与查询、构建侧共享。
    # L0/L1 分层索引：layers_index_enabled（默认 true）开时，取 vector_store 的
    # layers_l0/l1 具名实例（指向不同 collection = 分表）注入；未配置则该层传 None，
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

    return VectorIndexBuilder(
        vector_store=VectorProducer.dep(config, default="memory"),
        kv_store=KvProducer.dep(config, default="memory"),
        chunker=ChunkerProducer.dep(config, default="fixed_window"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        vector_l0=_opt_dep(VectorProducer, "layers_l0"),
        vector_l1=_opt_dep(VectorProducer, "layers_l1"),
    )
