# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内存版 :class:`~storage.entity_store.EntityStore`（F09 D9）。

生产代码此前只有 ES 实现，内存实现只存在于测试替身；分片实体包装层需要契约测试与
无外部服务的单测，因此把内存版收进实现目录并注册为 ``memory`` target。

能力：

- ``ATOMIC_UNLINK``：``UNLINK`` / ``DELETE_IF_EMPTY`` 在同一把记录锁内完成；
- ``GROUPED_LINKS``：关联按分片分组保存，``shards`` 过滤只返回选中分组的并集。

空记录保留但不出现在 ``find_*`` / ``search`` 结果里，由 ``DELETE_IF_EMPTY`` 或对账清理。
"""

from __future__ import annotations

import math
import threading

from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntitySearchResult,
    EntityStoreFilters,
)
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.capability import StoreCapability
from jiuwen_memory.storage.entity_store import EntityStore, EntityStoreProducer

_UNGROUPED = ""


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _matches_filters(record: EntityStoreFilters, filters: EntityStoreFilters) -> bool:
    for field_name in ("actor_id", "assistant_id", "session_id"):
        expected = getattr(filters, field_name)
        if expected is not None and getattr(record, field_name) != expected:
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


class _StoredEntity:
    """内部存储形态：逻辑 id + 记录字段 + 按分片分组的关联。"""

    __slots__ = ("record", "linked_by_shard")

    def __init__(self, record: EntityRecord, linked_by_shard: dict[str, list[str]]) -> None:
        self.record = record
        self.linked_by_shard = linked_by_shard

    def all_ids(self) -> tuple[str, ...]:
        collected: list[str] = []
        for ids in self.linked_by_shard.values():
            collected.extend(ids)
        return tuple(dict.fromkeys(collected))

    def view(self, shards: frozenset[str] | None) -> EntityRecord:
        if shards is None:
            linked = self.all_ids()
        else:
            selected: list[str] = []
            for shard, ids in self.linked_by_shard.items():
                if shard not in shards:
                    continue
                selected.extend(ids)
            linked = tuple(dict.fromkeys(selected))
        return EntityRecord(
            id=self.record.id,
            space_id=self.record.space_id,
            entity_text=self.record.entity_text,
            entity_type=self.record.entity_type,
            linked_memory_ids=linked,
            filters=self.record.filters,
            entity_text_hash=self.record.entity_text_hash,
            embedding=self.record.embedding,
        )


class InMemoryEntityStore(EntityStore):
    """纯内存实体反向索引：按 scope 原生隔离，单记录锁内完成增量操作。"""

    def __init__(self) -> None:
        self._records: dict[tuple[tuple[str, str, str, str, str], str], _StoredEntity] = {}
        self._lock = threading.RLock()

    def store_type(self) -> StoreType:
        return StoreType.ENTITY

    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset(
            {StoreCapability.ATOMIC_UNLINK, StoreCapability.GROUPED_LINKS}
        )

    def health(self) -> None:
        return None

    def ensure_index(self) -> None:
        return None

    # -- 查询 ---------------------------------------------------------------- #

    def find_by_entity_text_hash(
        self,
        scope: Scope,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        limit: int = 500,
    ) -> list[EntityRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        hashes = {value for value in entity_text_hashes if value}
        if not hashes:
            return []
        key = _scope_key(scope)
        out: list[EntityRecord] = []
        with self._lock:
            for (record_scope, _), stored in self._records.items():
                if record_scope != key:
                    continue
                if stored.record.entity_text_hash not in hashes:
                    continue
                if not _matches_filters(stored.record.filters, filters):
                    continue
                view = stored.view(shards)
                if shards is not None and not view.linked_memory_ids:
                    continue
                if not view.linked_memory_ids:
                    continue  # 空记录不返回
                out.append(view)
                if len(out) >= limit:
                    break
        return out

    @property
    def supports_complete_linked_enumeration(self) -> bool:
        return True

    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        complete: bool = False,
    ) -> list[EntityRecord]:
        key = _scope_key(scope)
        out: list[EntityRecord] = []
        with self._lock:
            for (record_scope, _), stored in self._records.items():
                if record_scope != key:
                    continue
                if not any(memory_id in ids for ids in stored.linked_by_shard.values()):
                    continue
                if not _matches_filters(stored.record.filters, filters):
                    continue
                view = stored.view(shards)
                if view.linked_memory_ids:
                    out.append(view)
        return out

    def search(
        self,
        scope: Scope,
        query_vector: list[float],
        *,
        top_k: int,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
    ) -> list[EntitySearchResult]:
        key = _scope_key(scope)
        scored: list[EntitySearchResult] = []
        with self._lock:
            for (record_scope, _), stored in self._records.items():
                if record_scope != key:
                    continue
                if not _matches_filters(stored.record.filters, filters):
                    continue
                view = stored.view(shards)
                if not view.linked_memory_ids or stored.record.embedding is None:
                    continue
                score = _cosine(query_vector, stored.record.embedding)
                if score > 0.0:
                    scored.append(EntitySearchResult(record=view, score=score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    # -- 变更 ---------------------------------------------------------------- #

    def execute_operations(
        self, scope: Scope, operations: list[EntityOperation]
    ) -> EntityBatchResult:
        key = _scope_key(scope)
        successful: list[str] = []
        failed: list[str] = []
        for op in operations:
            record_id = op.record_id or (op.record.id if op.record is not None else None)
            if not record_id:
                continue
            try:
                self._apply(key, op, record_id)
            except ConflictError:
                failed.append(record_id)
                continue
            successful.append(record_id)
        return EntityBatchResult(successful_ids=successful, failed_ids=failed)

    def _apply(
        self,
        key: tuple[str, str, str, str, str],
        op: EntityOperation,
        record_id: str,
    ) -> None:
        with self._lock:
            slot = (key, record_id)
            stored = self._records.get(slot)
            if op.type is EntityOpType.INSERT:
                if op.record is None:
                    return
                if stored is not None:
                    raise ConflictError("entity", record_id)
                grouped: dict[str, list[str]] = {}
                for memory_id in op.record.linked_memory_ids:
                    shard = op.memory_shards.get(memory_id, _UNGROUPED)
                    grouped.setdefault(shard, []).append(memory_id)
                self._records[slot] = _StoredEntity(op.record, grouped)
                return
            if stored is None:
                # UNLINK / DELETE_IF_EMPTY / DELETE 对不存在的记录幂等成功；
                # LINK 是 upsert：带模板时按模板创建，否则（没有文本字段可继承）失败。
                if op.type in (
                    EntityOpType.UNLINK,
                    EntityOpType.DELETE,
                    EntityOpType.DELETE_IF_EMPTY,
                ):
                    return
                if op.type is EntityOpType.LINK and op.record is not None:
                    grouped: dict[str, list[str]] = {}
                    for memory_id in op.link_memory_ids:
                        shard = op.memory_shards.get(memory_id, _UNGROUPED)
                        grouped.setdefault(shard, []).append(memory_id)
                    template = op.record
                    created = EntityRecord(
                        id=record_id,
                        space_id=template.space_id,
                        entity_text=template.entity_text,
                        entity_type=template.entity_type,
                        linked_memory_ids=tuple(op.link_memory_ids),
                        filters=template.filters,
                        entity_text_hash=template.entity_text_hash,
                        embedding=template.embedding,
                    )
                    self._records[slot] = _StoredEntity(created, grouped)
                    return
                raise ConflictError("entity", record_id)
            if op.type is EntityOpType.LINK:
                for memory_id in op.link_memory_ids:
                    shard = op.memory_shards.get(memory_id, _UNGROUPED)
                    bucket = stored.linked_by_shard.setdefault(shard, [])
                    if memory_id not in bucket:
                        bucket.append(memory_id)
                return
            if op.type is EntityOpType.UNLINK:
                scoped = {
                    memory_id: op.memory_shards[memory_id]
                    for memory_id in op.unlink_memory_ids
                    if memory_id in op.memory_shards
                }
                self._remove_ids(stored, set(op.unlink_memory_ids) - set(scoped))
                self._remove_from_groups(stored, scoped)
                return
            if op.type is EntityOpType.UNLINK_UPDATE:
                # 兼容旧路径：整体替换，保留原分组形状（存在覆盖竞态，仅过渡期使用）。
                if op.record is not None:
                    grouped: dict[str, list[str]] = {}
                    for memory_id in op.record.linked_memory_ids:
                        shard = op.memory_shards.get(memory_id, _UNGROUPED)
                        grouped.setdefault(shard, []).append(memory_id)
                    stored.linked_by_shard = grouped
                return
            if op.type is EntityOpType.DELETE_IF_EMPTY:
                if not stored.all_ids():
                    del self._records[slot]
                return
            if op.type is EntityOpType.DELETE:
                del self._records[slot]

    @staticmethod
    def _remove_from_groups(stored: _StoredEntity, scoped: dict[str, str]) -> None:
        """只从指定分组移除（GROUPED_LINKS：``UNLINK`` 携带 ``memory_shards`` 时的语义）。"""
        for memory_id, shard in scoped.items():
            bucket = stored.linked_by_shard.get(shard)
            if bucket is None or memory_id not in bucket:
                continue
            bucket.remove(memory_id)
            if not bucket:
                del stored.linked_by_shard[shard]

    @staticmethod
    def _remove_ids(stored: _StoredEntity, ids: set[str]) -> None:
        for shard in list(stored.linked_by_shard):
            remaining = [mid for mid in stored.linked_by_shard[shard] if mid not in ids]
            if remaining:
                stored.linked_by_shard[shard] = remaining
            else:
                del stored.linked_by_shard[shard]


@EntityStoreProducer.register("memory")
def _build(config):
    return InMemoryEntityStore()
