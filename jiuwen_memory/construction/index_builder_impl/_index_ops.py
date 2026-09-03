"""IndexBuilder 实现共用的索引投影与流水线辅助。

供 fulltext / vector / unified 等 IndexBuilder 实现按需调用：metadata 投影、Scope 分组、
端口解析、全文文档构造、向量切片-向量化-写库流水线、L0/L1 分层索引的建删。
批量编排（``IndexWriteMode`` 门控、删后重建顺序、跨形式组合）留在各 builder 内。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from jiuwen_memory.common.chunker.base import Chunker
from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.log import get_logger, redact_for_log, scope_for_log
from jiuwen_memory.common.type_def import (
    T_EVENT_UNKNOWN,
    T_INVALID_OPEN,
    Chunk,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.storage.fulltext import FulltextStore
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import Document, VectorRecord
from jiuwen_memory.storage.vector import VectorStore

logger = get_logger(__name__)

ScopeKey = tuple[str, str, str, str, str]

# ---------------------------------------------------------------------------
# Scope 辅助
# ---------------------------------------------------------------------------


def scope_key(scope: Scope) -> ScopeKey:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def scope_from_key(key: ScopeKey) -> Scope:
    return Scope(org=key[0], space=key[1], user=key[2], agent=key[3], session=key[4])


def group_units_by_scope(units: list[MemoryUnit]) -> list[tuple[Scope, list[MemoryUnit]]]:
    """按 Scope 保持输入顺序分组，满足 Storage 写接口的显式 scope 契约。"""
    groups: dict[ScopeKey, list[MemoryUnit]] = {}
    scopes: dict[ScopeKey, Scope] = {}
    for unit in units:
        key = scope_key(unit.scope)
        groups.setdefault(key, []).append(unit)
        scopes.setdefault(key, unit.scope)
    return [(scopes[key], scoped_units) for key, scoped_units in groups.items()]


# ---------------------------------------------------------------------------
# 端口解析
# ---------------------------------------------------------------------------


def fulltext_port(storage: Storage, name: str, enabled: bool) -> FulltextStore | None:
    if not enabled or not storage.has_fulltext_port(name):
        return None
    return storage.fulltext_port(name)


def vector_port(storage: Storage, name: str, enabled: bool) -> VectorStore | None:
    if not enabled or not storage.has_vector_port(name):
        return None
    return storage.vector_port(name)


# ---------------------------------------------------------------------------
# metadata 投影（全文与向量共用同一份可过滤投影）
# ---------------------------------------------------------------------------


def index_metadata(
    unit: MemoryUnit,
    *,
    layer: str,
    seq: int | None = None,
) -> dict[str, object]:
    """构造可过滤索引投影，保留双命名空间的逻辑路径；系统真源字段覆盖同名用户字段。

    ``metadata`` 值为 JSON 标量原生类型，后端据此建 double/boolean/keyword mapping
    并在 top-k 截断前原生下推。UnitReader 复核读的是同一个对象，两侧判定不分叉。
    """
    metadata = {
        **{f"system_metadata.{key}": value for key, value in unit.system_metadata.items()},
        **{f"user_metadata.{key}": value for key, value in unit.user_metadata.items()},
    }
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
    if seq is not None:
        metadata["seq"] = seq
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


# ---------------------------------------------------------------------------
# 全文索引投影与分层写删
# ---------------------------------------------------------------------------


def content_document(unit: MemoryUnit) -> Document:
    """L2 content 全文文档（id 沿用 unit.id，兼容删除/update）。"""
    return Document(
        id=unit.id,  # L2 文档沿用 unit.id（F01 允许短期保留旧 id 兼容删除/update）
        text=unit.content,
        metadata=index_metadata(unit, layer="l2"),
    )


def layer_document(unit: MemoryUnit, layer: str) -> Document:
    """L0/L1 分层文档：id={unit_id}:l0/:l1（对齐 F01 命名），text=layers 对应字段。"""
    text = unit.layers.l0 if layer == "l0" else unit.layers.l1
    return Document(
        id=f"{unit.id}:{layer}",
        text=text,
        metadata=index_metadata(unit, layer=layer),
    )


def upsert_document(store: FulltextStore, scope: Scope, doc: Document) -> None:
    """写全文文档：insert 失败 → update 兜底（分层索引按此约定写入）。"""
    try:
        store.insert(scope, [doc])
    except Exception as exc:
        logger.warning(
            "index_ops: fulltext insert failed for %s: error_type=%s, try update",
            doc.id,
            type(exc).__name__,
        )
        try:
            store.update(scope, [doc])
        except Exception as exc2:
            logger.error(
                "index_ops: fulltext update also failed for %s: error_type=%s",
                doc.id,
                type(exc2).__name__,
            )


def build_fulltext_layers(
    ports: Iterable[tuple[FulltextStore | None, str]], unit: MemoryUnit
) -> None:
    """对单个 unit 构建 L0/L1 全文分层索引（store 非空且 layers 文本非空才写独立 store）。"""
    for store, layer in ports:
        if store is None:
            continue  # 该层未注入，跳过
        text = unit.layers.l0 if layer == "l0" else unit.layers.l1
        if not (text or "").strip():
            continue  # 该 unit 无此层分层，跳过
        upsert_document(store, unit.scope, layer_document(unit, layer))


def delete_layer_documents(
    ports: Iterable[tuple[FulltextStore | None, str]], unit_id: str, scope: Scope
) -> None:
    """删除该 unit 的 L0/L1 分层全文文档（幂等）。store 为 None 的层跳过。"""
    for store, layer in ports:
        if store is None:
            continue
        try:
            store.delete(scope, [f"{unit_id}:{layer}"])
        except Exception as exc:
            logger.warning(
                "index_ops: delete layer %s doc failed for %s: error_type=%s",
                layer,
                unit_id[:8],
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# 向量索引流水线
# ---------------------------------------------------------------------------


def layer_record_id(unit_id: str, layer: str) -> str:
    """分层 record id：``{unit_id}-layer-l0`` / ``{unit_id}-layer-l1``（对齐 F01 命名），
    与 content 的 chunk id（``{unit_id}-{chunk_id}``）不冲突。
    """
    return f"{unit_id}-layer-{layer}"


def chunk_tracking_key(unit_id: str) -> str:
    """unit_id → KVStore 中 chunk_id 跟踪记录的 key。"""
    return f"/index/chunks/{unit_id}"


def vectorize_unit(
    chunker: Chunker, embedder: Embedder, unit: MemoryUnit
) -> list[tuple[Chunk, list[float]]]:
    """对单个 unit 切片并逐 chunk 向量化，返回 ``(chunk, vector)`` 对。

    切不出 chunk 或 embed 失败的 unit 返回空列表（不阻断整批）。
    VectorIndexBuilder 与 UnifiedIndexBuilder 共用同一切片-embed 管线。
    """
    logger.info(
        "index_ops: vectorizing unit id=%s tier=%s provenance=%s content=%s",
        unit.id[:8],
        unit.tier.value,
        unit.provenance,
        redact_for_log(unit.content),
    )
    chunks = chunker.chunk(
        text=unit.content,
        unit_id=unit.id,
        metadata={"tier": unit.tier.value},
    )
    if not chunks:
        return []
    try:
        vectors = embedder.embed([c.text for c in chunks])
    except Exception as exc:
        logger.warning(
            "index_ops: Embedder.embed failed for unit %s: error_type=%s",
            unit.id[:8],
            type(exc).__name__,
        )
        return []
    return list(zip(chunks, vectors))


def vectorize_units(
    chunker: Chunker, embedder: Embedder, units: list[MemoryUnit]
) -> tuple[dict[ScopeKey, list[VectorRecord]], list[tuple[Scope, str, list[str]]]]:
    """切片 → 向量化 → VectorRecord，按 scope 分组；附带 chunk 跟踪列表。

    切不出 chunk 或 embed 失败的 unit 跳过（不阻断整批）。
    """
    scope_groups: dict[ScopeKey, list[VectorRecord]] = {}
    chunk_tracking: list[tuple[Scope, str, list[str]]] = []
    for unit in units:
        pairs = vectorize_unit(chunker, embedder, unit)
        if not pairs:
            continue

        # 构建 VectorRecord；record id 只要求在当前 Scope 内唯一。
        chunk_ids: list[str] = []
        unit_records: list[VectorRecord] = []
        for chunk, vector in pairs:
            record_id = f"{unit.id}-{chunk.id}"
            unit_records.append(
                VectorRecord(
                    id=record_id,
                    vector=vector,
                    metadata=index_metadata(unit, layer="l2", seq=chunk.seq),
                )
            )
            chunk_ids.append(record_id)

        scope_groups.setdefault(scope_key(unit.scope), []).extend(unit_records)
        chunk_tracking.append((unit.scope, unit.id, chunk_ids))
    return scope_groups, chunk_tracking


def write_vector_index(
    vector_store: VectorStore, scope_groups: dict[ScopeKey, list[VectorRecord]]
) -> None:
    """按 scope 分组写入 VectorStore；insert 失败回退 update。"""
    for key, group_records in scope_groups.items():
        scope = scope_from_key(key)
        try:
            vector_store.insert(scope, group_records)
        except Exception as exc:
            logger.warning(
                "index_ops: VectorStore.insert failed for scope %s: error_type=%s, try update",
                scope_for_log(key),
                type(exc).__name__,
            )
            try:
                vector_store.update(scope, group_records)
            except Exception as exc2:
                logger.error(
                    "index_ops: VectorStore.update also failed for scope %s: error_type=%s",
                    scope_for_log(key),
                    type(exc2).__name__,
                )


def write_chunk_trackings(
    kv_store: KVStore, chunk_tracking: list[tuple[Scope, str, list[str]]]
) -> None:
    """chunk_id 跟踪写入 KVStore（已存在则 update，否则 insert）。"""
    for scope, unit_id, chunk_ids in chunk_tracking:
        kv_key = chunk_tracking_key(unit_id)
        try:
            if kv_store.exists(scope, kv_key):
                kv_store.update(scope, kv_key, json.dumps(chunk_ids).encode())
            else:
                kv_store.insert(scope, kv_key, json.dumps(chunk_ids).encode())
        except Exception as exc:
            logger.warning(
                "index_ops: KVStore chunk tracking write failed for %s: error_type=%s",
                kv_key,
                type(exc).__name__,
            )


def delete_tracked_chunks(
    kv_store: KVStore, vector_store: VectorStore, scope: Scope, unit_id: str
) -> None:
    """按 chunk 跟踪记录删除向量条目（跟踪记录本身保留，由调用方决定覆写或清除）。"""
    kv_key = chunk_tracking_key(unit_id)
    try:
        raw = kv_store.get(scope, kv_key)
        chunk_ids = json.loads(raw.decode())
        vector_store.delete(scope, chunk_ids)
    except Exception as exc:
        logger.warning(
            "index_ops: no chunk tracking found for %s: error_type=%s",
            unit_id,
            type(exc).__name__,
        )


def clear_chunk_tracking(kv_store: KVStore, scope: Scope, unit_id: str) -> None:
    """清除 chunk 跟踪 KV 记录（幂等）。"""
    try:
        kv_store.delete(scope, chunk_tracking_key(unit_id))
    except Exception:
        pass  # 幂等


def build_layer_vector_indexes(
    ports: Iterable[tuple[VectorStore | None, str]],
    embedder: Embedder,
    units: list[MemoryUnit],
) -> None:
    """对带 layers 的 unit 构建 L0/L1 向量索引（整段 embed，写独立 store）。

    双重判定：store 非空（注入了该层）且 layers 字段非空（该 unit 有分层）才执行，
    任一为空则跳过该层该 unit——不报错、不建空记录。
    """
    for store, layer in ports:
        if store is None:
            continue  # 该层未注入，跳过
        _build_one_vector_layer(store, layer, embedder, units)


def _build_one_vector_layer(
    store: VectorStore, layer: str, embedder: Embedder, units: list[MemoryUnit]
) -> None:
    """构建单层（L0 或 L1）向量索引：整段 embed → 按 scope 分组写独立 store。

    L0/L1 是 unit 级整体（不切片），一条 unit 在该层表最多一条 record。
    """
    pending: list[tuple[MemoryUnit, str]] = []  # (unit, text) 待 embed
    for unit in units:
        text = (unit.layers.l0 if layer == "l0" else unit.layers.l1) or ""
        if not text.strip():
            continue
        pending.append((unit, text))

    if not pending:
        return

    # 批量 embed 整段文本
    try:
        vectors = embedder.embed([t for _, t in pending])
    except Exception as exc:
        logger.warning(
            "index_ops: layers %s embed failed: error_type=%s",
            layer,
            type(exc).__name__,
        )
        return

    groups: dict[ScopeKey, list[VectorRecord]] = {}
    for (unit, _), vector in zip(pending, vectors):
        record = VectorRecord(
            id=layer_record_id(unit.id, layer),
            vector=vector,
            metadata=index_metadata(unit, layer=layer),
        )
        groups.setdefault(scope_key(unit.scope), []).append(record)

    write_vector_index(store, groups)


def delete_layer_vector_records(
    ports: Iterable[tuple[VectorStore | None, str]], unit_id: str, scope: Scope
) -> None:
    """删除该 unit 的 L0/L1 分层 record（幂等）。store 为 None 的层跳过。"""
    for store, layer in ports:
        if store is None:
            continue
        try:
            store.delete(scope, [layer_record_id(unit_id, layer)])
        except Exception as exc:
            logger.warning(
                "index_ops: delete layer %s record failed for %s: error_type=%s",
                layer,
                unit_id[:8],
                type(exc).__name__,
            )


def clear_unit_vector_index(
    kv_store: KVStore | None,
    vector_store: VectorStore | None,
    layer_ports: Iterable[tuple[VectorStore | None, str]],
    scope: Scope,
    unit_id: str,
    *,
    clear_tracking: bool,
) -> None:
    """删除单个 unit 的向量索引条目 + L0/L1 record（幂等容错）。

    ``clear_tracking=True``（remove 路径）连同 chunk 跟踪记录一并清除；
    ``False``（update 路径）保留跟踪记录，由后续重建覆写。
    vector/kv 任一缺失时只清分层 record。
    """
    if vector_store is None or kv_store is None:
        delete_layer_vector_records(layer_ports, unit_id, scope)
        return
    delete_tracked_chunks(kv_store, vector_store, scope, unit_id)
    if clear_tracking:
        clear_chunk_tracking(kv_store, scope, unit_id)
    delete_layer_vector_records(layer_ports, unit_id, scope)
