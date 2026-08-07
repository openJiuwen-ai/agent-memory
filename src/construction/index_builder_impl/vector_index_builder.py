"""最小实现：:class:`~construction.index_builder.IndexBuilder`。

把记忆单元的 content 经注入的 :class:`~common.chunker.base.Chunker` 切片
→ :class:`~common.embedder.base.Embedder` 向量化 → 写入
:class:`~storage.vector.VectorStore`（向量 ANN 索引）。
同时在 :class:`~storage.kv.KVStore` 维护 chunk_id 跟踪记录（update/remove 时
读取旧 chunk_id 列表）。删除入口接收 ``MemoryUnit``，直接使用其 scope 定位索引。

VectorRecord.id 采用 ``{unit.id}-{chunk.id}`` 拼接格式，在记录所属 Scope 内唯一。
"""

from __future__ import annotations

import json

from common.chunker.base import Chunker, ChunkerProducer
from common.embedder.base import Embedder, EmbedderProducer
from common.log import get_logger
from common.type_def import T_INVALID_OPEN, MemoryUnit, Scope
from construction.base import OperatorType
from construction.index_builder import IndexBuilder, IndexBuilderProducer
from storage.storage import Storage, StorageProducer
from storage.types import VectorRecord
from storage.vector import VectorStore

logger = get_logger(__name__)

_ScopeKey = tuple[str, str, str, str, str]


def _scope_key(scope: Scope) -> _ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _scope_from_key(key: _ScopeKey) -> Scope:
    return Scope(org=key[0], space=key[1], user=key[2], agent=key[3], session=key[4])


def _index_metadata(
    unit: MemoryUnit,
    *,
    layer: str,
    seq: int | None = None,
) -> dict[str, object]:
    """构造可过滤索引投影；用户 metadata 原样带入，系统真源字段随后覆盖。

    ``metadata`` 值为 JSON 标量原生类型，后端据此在 top-k 截断前原生比较。
    UnitReader 复核读的是同一个对象，两侧判定一致。
    """
    metadata = dict(unit.metadata)
    metadata.update(
        {
            "unit_id": unit.id,
            "tier": unit.tier.value,
            "lifecycle": unit.lifecycle.value,
            "tags": list(unit.tags),
            "source": unit.source.value,
            "content_layer": layer,
        }
    )
    if seq is not None:
        metadata["seq"] = seq
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


class VectorIndexBuilder(IndexBuilder):
    """向量索引构建：MemoryUnit → Chunker → Embedder → VectorStore + chunk tracking。

    L0/L1 分层索引（架构 §9.1）：``unit.layers.l0``/``.l1`` 非空且对应 store 已注入时，
    对整段 l0/l1 文本（不切片）做 embed，写独立 VectorStore 实例（不同 collection = 分表），
    record id = ``{unit_id}-layer-l0``/``{unit_id}-layer-l1``。store 为 None 时跳过该层，
    不影响 content。
    """

    def __init__(
        self,
        storage: Storage,
        chunker: Chunker,
        embedder: Embedder,
        *,
        layers_enabled: bool = True,
    ) -> None:
        self._vector_store = storage.vector if storage.has_vector() else None
        self._kv_store = storage.kv if storage.has_kv() else None
        self._chunker = chunker
        self._embedder = embedder
        # L0/L1 分层 store：None 表示不构建该层索引（向后兼容 + 配置降级）。
        self._vector_l0 = _vector_port(storage, "layers_l0", layers_enabled)
        self._vector_l1 = _vector_port(storage, "layers_l1", layers_enabled)

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

    def build(self, units: list[MemoryUnit]) -> None:
        """为一批记忆单元构建向量索引。"""
        logger.info("VectorIndexBuilder: building index for %d units", len(units))
        if self._vector_store is None:
            self._build_layers(units)
            return
        scope_groups: dict[_ScopeKey, list[VectorRecord]] = {}
        chunk_tracking: list[tuple[Scope, str, list[str]]] = []

        for unit in units:
            logger.info(
                "VectorIndexBuilder: indexing unit id=%s tier=%s provenance=%s content=%s",
                unit.id[:8],
                unit.tier.value,
                unit.provenance,
                unit.content[:200],
            )
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
                logger.warning(
                    "VectorIndexBuilder: Embedder.embed failed for unit %s: %s",
                    unit.id[:8],
                    exc,
                )
                continue

            # 构建 VectorRecord；record id 只要求在当前 Scope 内唯一。
            chunk_ids: list[str] = []
            unit_records: list[VectorRecord] = []
            for chunk, vector in zip(chunks, vectors):
                record_id = f"{unit.id}-{chunk.id}"
                record = VectorRecord(
                    id=record_id,
                    vector=vector,
                    metadata=_index_metadata(unit, layer="l2", seq=chunk.seq),
                )
                unit_records.append(record)
                chunk_ids.append(record_id)

            scope_groups.setdefault(_scope_key(unit.scope), []).extend(unit_records)
            chunk_tracking.append((unit.scope, unit.id, chunk_ids))

        # L0/L1 分层索引：store 非空且 layers 非空才整段 embed 写独立 store（分表）。
        # 必须在下方空结果判断之前执行——content 切不出 chunk 时没有 L2 record，
        # 但 unit 的 layers.l0/l1 仍可能非空、仍需建分层索引（update
        # 路径亦依赖此处按新 layers 重建，见 update 的删旧→重建约定）。
        self._build_layers(units)

        if not scope_groups:
            return

        for key, group_records in scope_groups.items():
            scope = _scope_from_key(key)
            try:
                self._vector_store.insert(scope, group_records)
            except Exception as exc:
                logger.warning(
                    "VectorIndexBuilder: VectorStore.insert failed for scope %s: %s", key, exc
                )
                try:
                    self._vector_store.update(scope, group_records)
                except Exception as exc2:
                    logger.error(
                        "VectorIndexBuilder: VectorStore.update also failed for scope %s: %s",
                        key,
                        exc2,
                    )

        # chunk_id 跟踪写入 KVStore
        if self._kv_store is None:
            return
        for scope, unit_id, chunk_ids in chunk_tracking:
            kv_key = self._chunk_tracking_key(unit_id)
            try:
                if self._kv_store.exists(scope, kv_key):
                    self._kv_store.update(scope, kv_key, json.dumps(chunk_ids).encode())
                else:
                    self._kv_store.insert(scope, kv_key, json.dumps(chunk_ids).encode())
            except Exception as exc:
                logger.warning(
                    "VectorIndexBuilder: KVStore chunk tracking write failed for %s: %s",
                    kv_key,
                    exc,
                )

    def update(self, units: list[MemoryUnit]) -> None:
        """增量更新向量索引：先删旧 chunk + 旧 L0/L1 record → 再建新。

        SUPERSEDE/UPDATE 场景下 unit 可能从「有 layers」变「无 layers」（或反之），故 L0/L1
        必须先删旧 record（store 非空才删），再由 build 按新 layers 决定是否重建——否则
        旧 L0/L1 残留。
        """
        if self._vector_store is None or self._kv_store is None:
            for unit in units:
                self._delete_layer_records(unit.id, unit.scope)
            self._build_layers(units)
            return
        for unit in units:
            kv_key = self._chunk_tracking_key(unit.id)
            try:
                raw = self._kv_store.get(unit.scope, kv_key)
                old_chunk_ids = json.loads(raw.decode())
                self._vector_store.delete(unit.scope, old_chunk_ids)
            except Exception as exc:
                logger.warning(
                    "VectorIndexBuilder: no old chunks found for unit %s: %s",
                    unit.id[:8],
                    exc,
                )
            # 先删旧 L0/L1 record（幂等），build 会按新 layers 重建
            self._delete_layer_records(unit.id, unit.scope)

        self.build(units)

    def remove(self, units: list[MemoryUnit]) -> None:
        """删除一批记忆单元对应的向量索引条目（幂等）。"""
        for unit in units:
            self._remove_by_scope(unit.id, unit.scope)

    def rebuild(self) -> None:
        # 最小实现：索引与真源同生命周期，无独立重建路径。
        return None

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def remove_with_scope(self, unit_ids: list[str], scope: Scope) -> None:
        """已知 scope 时直接删除索引条目，避免 lookup。"""
        for unit_id in unit_ids:
            self._remove_by_scope(unit_id, scope)

    def _remove_by_scope(self, unit_id: str, scope: Scope) -> None:
        """删除单个 unit 的向量索引条目 + chunk tracking + L0/L1 record。"""
        if self._vector_store is None or self._kv_store is None:
            self._delete_layer_records(unit_id, scope)
            return
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

    def _build_layers(self, units: list[MemoryUnit]) -> None:
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

    def _build_one_layer(
        self, store: VectorStore, layer: str, get_text, units: list[MemoryUnit]
    ) -> None:
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
        groups: dict[_ScopeKey, list[VectorRecord]] = {}
        for (unit, text), vector in zip(pending, vectors):
            record = VectorRecord(
                id=self._layer_record_id(unit.id, layer),
                vector=vector,
                metadata=_index_metadata(unit, layer=layer),
            )
            key = _scope_key(unit.scope)
            groups.setdefault(key, []).append(record)

        for key, records in groups.items():
            scope = _scope_from_key(key)
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
    return VectorIndexBuilder(
        storage=StorageProducer.resolve(config),
        chunker=ChunkerProducer.dep(config, default="fixed_window"),
        embedder=EmbedderProducer.dep(config, default="hashing"),
        layers_enabled=config.get("layers_index_enabled", True),
    )


def _vector_port(storage: Storage, name: str, enabled: bool) -> VectorStore | None:
    if not enabled or not storage.has_vector_port(name):
        return None
    return storage.vector_port(name)
