# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``shard_routed`` 实体存储（F09 D9）。

两种模式，构建层看到的指令与返回的 :class:`EntityRecord` 完全一致：

- ``shared``：单索引，关联按分片分组（``linked_shards`` / ``linked_by_shard``）；
  ``INSERT`` / ``LINK`` 经 ``ShardLocator`` 给每个关联 id 标注分片，``shards`` 过滤在
  实体记录截断（``limit`` 按实体记录数计）之前下推；``UNLINK`` 不需要定位。
- ``split``：每个分片一个实体索引，对外 id 是**逻辑 id**，物理文档 id 由
  ``{逻辑 id}@{分片}`` 确定性派生。``find_by_entity_text_hash`` 按逻辑 id 聚合，
  对聚合结果执行 ``DELETE`` / ``UNLINK`` / ``DELETE_IF_EMPTY`` 作用于全部物理文档。

``UNLINK`` / ``DELETE_IF_EMPTY`` 的原子性由内层 ``ATOMIC_UNLINK`` 能力保证，包装层
启动时校验；``shared`` 模式还要求内层声明 ``GROUPED_LINKS``。
"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import (
    BackendError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntitySearchResult,
    EntityStoreFilters,
)
from jiuwen_memory.storage.base import BaseStore, StoreType
from jiuwen_memory.storage.capability import StoreCapability, require_capability
from jiuwen_memory.storage.entity_store import EntityStore, EntityStoreProducer
from jiuwen_memory.storage.security import CompositeStoreSecurity
from jiuwen_memory.storage.shard_routing import ShardLocation, ShardLocator, ShardResolver
from jiuwen_memory.storage.shard_search import (
    LazyShardLocator,
    require_distinct_shard_resources,
)

logger = get_logger(__name__)

_PHYSICAL_SEPARATOR = "@"


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def logical_id_of(record_id: str) -> str:
    """从 ``{逻辑 id}@{分片}`` 反解逻辑 id；不含分隔符时原样返回。"""
    return record_id.rsplit(_PHYSICAL_SEPARATOR, 1)[0]


def physical_id(logical_id: str, shard: str) -> str:
    """确定性物理文档 id。"""
    return f"{logical_id}{_PHYSICAL_SEPARATOR}{shard}"


def _find_linked(
    store: EntityStore,
    scope: Scope,
    memory_id: str,
    *,
    filters: EntityStoreFilters,
    shards: frozenset[str] | None = None,
    complete: bool = False,
) -> list[EntityRecord]:
    """按需传递 ``complete``：普通反查不带该参数，兼容未声明搬迁能力的第三方插件。

    只有搬迁（``complete=True``）才要求完整枚举；插件不支持时给出可诊断的能力错误，
    而不是原始的 ``TypeError``。
    """
    if not complete:
        return store.find_by_linked_memory_id(
            scope, memory_id, filters=filters, shards=shards
        )
    if not store.supports_complete_linked_enumeration:
        # 不用 try/except TypeError 猜测：插件内部真正的 TypeError 不应被误报为
        # 能力缺失，只有显式声明支持的实现才会收到 complete=True。
        raise UnsupportedStorageCapabilityError(
            f"{type(store).__name__} does not support complete enumeration of linked "
            "memories required for relocation"
        )
    return store.find_by_linked_memory_id(
        scope, memory_id, filters=filters, shards=shards, complete=True
    )


class ShardRoutedEntityStore(EntityStore):
    """实体端口的 ``shard_routed`` 包装层。"""

    def __init__(
        self,
        *,
        resolver: ShardResolver,
        locator: ShardLocator,
        mode: str = "split",
        stores: dict[str, EntityStore] | None = None,
        inner: EntityStore | None = None,
        owned: tuple[BaseStore, ...] = (),
    ) -> None:
        if mode not in ("split", "shared"):
            raise ValidationError(f"entity shard mode must be split|shared, got {mode!r}")
        if mode == "shared":
            if inner is None or stores:
                raise ValidationError(
                    "shared entity shard mode requires 'inner' and no 'tables'"
                )
            require_capability(
                inner, StoreCapability.ATOMIC_UNLINK, role="entity shared inner"
            )
            require_capability(
                inner, StoreCapability.GROUPED_LINKS, role="entity shared inner"
            )
        else:
            if inner is not None or not stores:
                raise ValidationError("split entity shard mode requires per-shard stores")
            missing = [name for name in resolver.shards if name not in stores]
            if missing:
                raise ValidationError(f"entity split mode is missing shards: {missing}")
            unknown = [name for name in stores if name not in resolver.shards]
            if unknown:
                raise ValidationError(f"entity split mode has unconfigured shards: {unknown}")
            for shard, store in stores.items():
                require_capability(
                    store, StoreCapability.ATOMIC_UNLINK, role=f"entity shard {shard}"
                )
            require_distinct_shard_resources(stores, kind="entity")
        self._resolver = resolver
        self._locator = locator
        self._mode = mode
        self._stores = dict(stores or {})
        self._inner = inner
        self._owned = tuple(owned)

    # -- 自描述 --------------------------------------------------------------- #

    @property
    def security(self):
        return CompositeStoreSecurity(
            [self._inner] if self._inner is not None else list(self._stores.values())
        )

    def store_type(self) -> StoreType:
        return StoreType.ENTITY

    def capabilities(self) -> frozenset[StoreCapability]:
        """split 模式不声明 ``SHARD_PREFILTER``：它靠选表而不是单索引内前置过滤。

        与向量 / 全文包装层同一口径——只有 shared 才有「把分片条件下推到单索引并在
        截断前求值」这件事，声明了就必须履约（S09 #6）。``SHARD_RELOCATABLE`` 只在
        内层完整枚举时才声明，避免不兼容插件拖到第一次搬迁才失败。
        """
        caps = {
            StoreCapability.ATOMIC_UNLINK,
            StoreCapability.GROUPED_LINKS,
            StoreCapability.SHARD_ROUTED,
        }
        if all(
            store.supports_complete_linked_enumeration
            for store in self._physical_stores()
        ):
            caps.add(StoreCapability.SHARD_RELOCATABLE)
        if self._mode == "shared":
            caps.add(StoreCapability.SHARD_PREFILTER)
        return frozenset(caps)

    @property
    def shards(self) -> tuple[str, ...]:
        return self._resolver.shards

    def shard_descriptors(self, kind: str = "entity") -> dict[str, dict[str, str]]:
        """每个分片的物理标识（shared 模式下每个分片共享同一实例身份）。

        ``kind`` 是登记键（``{capability}:{port_name}``），由装配器传入。
        """
        if self._inner is not None:
            descriptor = self._inner.resource_descriptor()
            if descriptor is None:
                return {}
            return {shard: {kind: descriptor} for shard in self.shards}
        out: dict[str, dict[str, str]] = {}
        for shard, store in self._stores.items():
            descriptor = store.resource_descriptor()
            if descriptor is not None:
                out[shard] = {kind: descriptor}
        return out

    def locator_name(self) -> str | None:
        """注入的定位端口名（装配期校验能力用）。"""
        provider = getattr(self._locator, "port_name", None)
        return provider() if callable(provider) else None

    def bind_shard_locator(self, locator: Any) -> None:
        """装配期绑定定位端口（S09 #3 / #4）；由 store manager 在端口建好后调用。"""
        bind = getattr(self._locator, "bind", None)
        if not callable(bind):
            raise ValidationError(
                f"{type(self).__name__} locator does not support assembly-time binding"
            )
        bind(locator)

    def health(self) -> None:
        if self._inner is not None:
            self._inner.health()
            return
        for store in self._stores.values():
            store.health()

    def connection_dependencies(self) -> tuple[BaseStore, ...]:
        """物理分片实体索引 / 共享 inner 的连接预算来源（内联实例不在端口表里）。"""
        targets = [self._inner] if self._inner is not None else list(self._stores.values())
        return tuple(store for store in targets if isinstance(store, BaseStore))

    def close(self) -> None:
        for store in self._owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        self._owned = ()

    def _physical_stores(self) -> list[EntityStore]:
        if self._inner is not None:
            return [self._inner]
        return list(self._stores.values())

    # -- 搬迁（F09 D16） ------------------------------------------------------ #

    def _filters(self, scope: Scope) -> EntityStoreFilters:
        return EntityStoreFilters.from_scope(scope)

    def relocate_unit(
        self, scope: Scope, unit_id: str, source: str, target: str
    ) -> None:
        """把该记忆与实体的关联复制到目标分片（不删源，幂等）。

        共享模式只**追加**目标分组（单文档原子 LINK），源分组原样保留到清理阶段：
        任何时刻该记忆至少在一个分组里，崩溃后仍能经 ``find_by_linked_memory_id``
        找回实体做回滚或继续清理，而不是先 UNLINK 再 LINK、中间崩溃就丢掉关联。
        后端按条报告的失败一律抛错，由搬迁状态机回滚，不当作成功切换归属。
        """
        filters = self._filters(scope)
        if self._inner is not None:
            records = _find_linked(
                self._inner, scope, unit_id, filters=filters, complete=True
            )
            ops = [
                EntityOperation(
                    type=EntityOpType.LINK,
                    record_id=record.id,
                    link_memory_ids=(unit_id,),
                    record=record,
                    memory_shards={unit_id: target},
                )
                for record in records
            ]
            if ops:
                _require_all_succeeded(
                    self._inner.execute_operations(scope, ops), "relocate link"
                )
            return
        records = self.find_by_linked_memory_id(
            scope, unit_id, filters=filters, complete=True
        )
        for record in records:
            location = self._locator.locate(scope, list(record.linked_memory_ids))
            ids = [
                uid
                for uid in record.linked_memory_ids
                if uid != unit_id and location.found.get(uid) == target
            ]
            ids.append(unit_id)
            physical = physical_id(record.id, target)
            result = self._stores[target].execute_operations(
                scope,
                [
                    EntityOperation(
                        type=EntityOpType.LINK,
                        record_id=physical,
                        link_memory_ids=tuple(ids),
                        record=_with_links(record, physical, ids),
                        memory_shards={uid: target for uid in ids},
                    )
                ],
            )
            _require_all_succeeded(result, "relocate link")

    def cleanup_unit(self, scope: Scope, unit_id: str, shard: str) -> None:
        """只清理指定分片中该记忆的关联（幂等）。

        共享模式按分组 UNLINK（``memory_shards`` 指定分组，GROUPED_LINKS 契约），只移除
        ``shard`` 分组里的这条关联；拆分模式在该分片的物理文档上 UNLINK，空文档同批
        ``DELETE_IF_EMPTY``。后端按条失败时抛错，调用方保留清理标记下次重试。
        """
        filters = self._filters(scope)
        if self._inner is not None:
            records = _find_linked(
                self._inner, scope, unit_id, filters=filters, complete=True
            )
            ops = [
                EntityOperation(
                    type=EntityOpType.UNLINK,
                    record_id=record.id,
                    unlink_memory_ids=(unit_id,),
                    memory_shards={unit_id: shard},
                )
                for record in records
            ]
            if ops:
                _require_all_succeeded(
                    self._inner.execute_operations(scope, ops), "cleanup unlink"
                )
            return
        for record in _find_linked(
            self._stores[shard],
            scope,
            unit_id,
            filters=filters,
            complete=True,
        ):
            # 解除关联后若该物理文档已无任何关联，同批原子删除（内层声明 ATOMIC_UNLINK，
            # DELETE_IF_EMPTY 对仍有关联的文档是 no-op），避免源分片积累空实体文档。
            result = self._stores[shard].execute_operations(
                scope,
                [
                    EntityOperation(
                        type=EntityOpType.UNLINK,
                        record_id=record.id,
                        unlink_memory_ids=(unit_id,),
                    ),
                    EntityOperation(
                        type=EntityOpType.DELETE_IF_EMPTY,
                        record_id=record.id,
                    ),
                ],
            )
            _require_all_succeeded(result, "cleanup unlink")

    def ensure_index(self) -> None:
        if self._inner is not None:
            self._inner.ensure_index()
            return
        failures: list[str] = []
        for shard, store in self._stores.items():
            try:
                store.ensure_index()
            except Exception:  # noqa: BLE001 - 收集全部失败分片后整体失败
                failures.append(shard)
        if failures:
            raise UnsupportedStorageCapabilityError(
                f"entity ensure_index failed for shards: {failures}"
            )

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
        if self._inner is not None:
            return self._inner.find_by_entity_text_hash(
                scope, entity_text_hashes, filters=filters, shards=shards, limit=limit
            )
        selected = self._selected(shards)
        merged: dict[str, EntityRecord] = {}
        for shard in selected:
            for record in self._stores[shard].find_by_entity_text_hash(
                scope, entity_text_hashes, filters=filters, limit=limit
            ):
                logical = logical_id_of(record.id)
                previous = merged.get(logical)
                merged[logical] = _merge_records(previous, record, logical)
        return list(merged.values())[:limit]

    @property
    def supports_complete_linked_enumeration(self) -> bool:
        return all(
            store.supports_complete_linked_enumeration
            for store in self._physical_stores()
        )

    def find_by_linked_memory_id(
        self,
        scope: Scope,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
        complete: bool = False,
    ) -> list[EntityRecord]:
        if self._inner is not None:
            return _find_linked(
                self._inner,
                scope,
                memory_id,
                filters=filters,
                shards=shards,
                complete=complete,
            )
        located = self._locator.locate(scope, [memory_id])
        shard = located.found.get(memory_id)
        selected = self._selected(shards)
        targets = [shard] if shard in selected else list(selected)
        merged: dict[str, EntityRecord] = {}
        for target in targets:
            store = self._stores[target]
            for record in _find_linked(
                store,
                scope,
                memory_id,
                filters=filters,
                complete=complete,
            ):
                logical = logical_id_of(record.id)
                previous = merged.get(logical)
                merged[logical] = _merge_records(previous, record, logical)
        records = list(merged.values())
        return records

    def search(
        self,
        scope: Scope,
        query_vector: list[float],
        *,
        top_k: int,
        filters: EntityStoreFilters,
        shards: frozenset[str] | None = None,
    ) -> list[EntitySearchResult]:
        if self._inner is not None:
            return self._inner.search(
                scope, query_vector, top_k=top_k, filters=filters, shards=shards
            )
        selected = self._selected(shards)
        best: dict[str, EntitySearchResult] = {}
        for shard in selected:
            for hit in self._stores[shard].search(
                scope, query_vector, top_k=top_k, filters=filters
            ):
                logical = logical_id_of(hit.record.id)
                previous = best.get(logical)
                merged_record = _merge_records(
                    previous.record if previous else None, hit.record, logical
                )
                if previous is None or hit.score > previous.score:
                    best[logical] = EntitySearchResult(record=merged_record, score=hit.score)
        return sorted(best.values(), key=lambda item: item.score, reverse=True)[:top_k]

    # -- 变更 ---------------------------------------------------------------- #

    def execute_operations(
        self, scope: Scope, operations: list[EntityOperation]
    ) -> EntityBatchResult:
        if self._inner is not None:
            prepared, failed = self._annotate(scope, operations)
            result = self._inner.execute_operations(scope, prepared)
            return EntityBatchResult(
                successful_ids=list(result.successful_ids),
                failed_ids=[*result.failed_ids, *failed],
            )
        prepared, failed, probes = self._prepare_split(scope, operations)
        successful: list[str] = []
        for shard, group in prepared.items():
            result = self._stores[shard].execute_operations(scope, group)
            successful.extend(result.successful_ids)
            for failed_id in result.failed_ids:
                probe = probes.get(failed_id)
                if probe is not None and self._physical_exists(scope, shard, failed_id, probe):
                    # INSERT 重试命中已存在的物理文档：视为已完成（幂等）
                    successful.append(failed_id)
                else:
                    failed.append(failed_id)
        seen_success: list[str] = []
        for physical in successful:
            logical = logical_id_of(physical)
            if logical not in seen_success:
                seen_success.append(logical)
        seen_failed: list[str] = []
        for physical in failed:
            logical = logical_id_of(physical)
            if logical not in seen_failed:
                seen_failed.append(logical)
        # 一个逻辑实体对应多份物理文档：任一份失败即整条报失败（重试幂等：LINK 去重追加、
        # INSERT 命中已存在视为完成），避免同一逻辑 id 同时出现在成功与失败里。
        seen_success = [logical for logical in seen_success if logical not in seen_failed]
        return EntityBatchResult(successful_ids=seen_success, failed_ids=seen_failed)

    # -- shared：为每个关联标注分片 ------------------------------------------- #

    def _annotate(
        self, scope: Scope, operations: list[EntityOperation]
    ) -> tuple[list[EntityOperation], list[str]]:
        need_locate: list[str] = []
        for op in operations:
            if op.type in (EntityOpType.INSERT, EntityOpType.LINK):
                if op.record is not None:
                    need_locate.extend(op.record.linked_memory_ids)
                need_locate.extend(op.link_memory_ids)
            if op.type is EntityOpType.LINK:
                need_locate.extend(op.memory_shards)
        location = self._locator.locate(scope, list(dict.fromkeys(need_locate)))
        prepared: list[EntityOperation] = []
        failed: list[str] = []
        for op in operations:
            ids: tuple[str, ...] = ()
            if op.record is not None:
                ids = op.record.linked_memory_ids
            if op.link_memory_ids:
                ids = tuple(dict.fromkeys((*ids, *op.link_memory_ids)))
            missing = [uid for uid in ids if uid not in location.found]
            moving = [uid for uid in ids if uid in location.moving]
            if moving:
                failed.append(op.record_id or (op.record.id if op.record else ""))
                continue
            if op.type in (EntityOpType.INSERT, EntityOpType.LINK) and missing:
                failed.append(op.record_id or (op.record.id if op.record else ""))
                continue
            mapping = {uid: location.found[uid] for uid in ids if uid in location.found}
            prepared.append(
                EntityOperation(
                    type=op.type,
                    record=op.record,
                    record_id=op.record_id,
                    link_memory_ids=op.link_memory_ids,
                    unlink_memory_ids=op.unlink_memory_ids,
                    memory_shards=mapping,
                )
            )
        return prepared, failed

    # -- split：确定性物理 id 投影 -------------------------------------------- #

    def _physical_exists(
        self, scope: Scope, shard: str, physical: str, probe_id: str
    ) -> bool:
        records = self._stores[shard].find_by_linked_memory_id(
            scope, probe_id, filters=EntityStoreFilters()
        )
        return any(record.id == physical for record in records)

    def _prepare_split(
        self, scope: Scope, operations: list[EntityOperation]
    ) -> tuple[dict[str, list[EntityOperation]], list[str], dict[str, str]]:
        locate_ids: list[str] = []
        for op in operations:
            if op.type is EntityOpType.INSERT and op.record is not None:
                locate_ids.extend(op.record.linked_memory_ids)
            elif op.type is EntityOpType.LINK:
                locate_ids.extend(op.link_memory_ids)
        location = self._locator.locate(scope, list(dict.fromkeys(locate_ids)))
        prepared: dict[str, list[EntityOperation]] = {}
        failed: list[str] = []
        probes: dict[str, str] = {}
        for op in operations:
            failure_id = op.record_id or (op.record.id if op.record is not None else "")
            affected: tuple[str, ...] = ()
            if op.record is not None:
                affected = op.record.linked_memory_ids
            if op.link_memory_ids:
                affected = tuple((*affected, *op.link_memory_ids))
            if any(uid in location.moving for uid in affected):
                failed.append(failure_id)
                continue
            if op.type is EntityOpType.INSERT:
                if op.record is None:
                    continue
                grouped = self._group_ids(scope, op.record.linked_memory_ids, location)
                if grouped is None:
                    failed.append(failure_id)
                    continue
                for shard, ids in grouped.items():
                    physical = physical_id(op.record.id, shard)
                    record = _with_links(op.record, physical, ids)
                    prepared.setdefault(shard, []).append(
                        EntityOperation(
                            type=EntityOpType.INSERT,
                            record=record,
                            memory_shards={uid: shard for uid in ids},
                        )
                    )
                    if ids:
                        probes[physical] = ids[0]
                continue
            logical = logical_id_of(failure_id)
            if op.type is EntityOpType.LINK:
                grouped = self._group_ids(scope, op.link_memory_ids, location)
                if grouped is None:
                    failed.append(failure_id)
                    continue
                # 契约里 LINK 的 record 是可选的：带 record 时内层按 upsert 处理（缺失分片
                # 用它创建物理文档）；不带时只对已存在的物理文档增量追加，缺失分片由内层
                # 按条失败（首次关联到新分片必须提供 record）。
                template = op.record
                for shard, ids in grouped.items():
                    physical = physical_id(logical, shard)
                    prepared.setdefault(shard, []).append(
                        EntityOperation(
                            type=EntityOpType.LINK,
                            record_id=physical,
                            link_memory_ids=ids,
                            record=(
                                _with_links(template, physical, ids)
                                if template is not None
                                else None
                            ),
                            memory_shards={uid: shard for uid in ids},
                        )
                    )
                continue
            if op.type is EntityOpType.UNLINK:
                for shard in self.shards:
                    prepared.setdefault(shard, []).append(
                        EntityOperation(
                            type=EntityOpType.UNLINK,
                            record_id=physical_id(logical, shard),
                            unlink_memory_ids=op.unlink_memory_ids,
                        )
                    )
                continue
            if op.type in (EntityOpType.DELETE_IF_EMPTY, EntityOpType.DELETE):
                for shard in self.shards:
                    prepared.setdefault(shard, []).append(
                        EntityOperation(
                            type=op.type,
                            record_id=physical_id(logical, shard),
                        )
                    )
                continue
            failed.append(failure_id)
        return prepared, failed, probes

    @staticmethod
    def _group_ids(
        scope: Scope, ids: tuple[str, ...], location: ShardLocation
    ) -> dict[str, list[str]] | None:
        grouped: dict[str, list[str]] = {}
        for uid in ids:
            shard = location.found.get(uid)
            if shard is None:
                return None
            grouped.setdefault(shard, []).append(uid)
        return grouped

    # -- 内部 ---------------------------------------------------------------- #

    def _selected(self, shards: frozenset[str] | None) -> tuple[str, ...]:
        if shards is None:
            return self.shards
        selected = tuple(name for name in self.shards if name in shards)
        unknown = [name for name in shards if name not in self.shards]
        if unknown:
            raise ValidationError(
                f"unknown shard(s) {sorted(unknown)}; configured: {list(self.shards)}"
            )
        return selected


def _merge_records(
    previous: EntityRecord | None, record: EntityRecord, logical: str
) -> EntityRecord:
    if previous is None:
        return EntityRecord(
            id=logical,
            space_id=record.space_id,
            entity_text=record.entity_text,
            entity_type=record.entity_type,
            linked_memory_ids=tuple(record.linked_memory_ids),
            filters=record.filters,
            entity_text_hash=record.entity_text_hash,
            embedding=record.embedding,
        )
    return EntityRecord(
        id=logical,
        space_id=previous.space_id or record.space_id,
        entity_text=previous.entity_text or record.entity_text,
        entity_type=previous.entity_type or record.entity_type,
        linked_memory_ids=tuple(
            dict.fromkeys((*previous.linked_memory_ids, *record.linked_memory_ids))
        ),
        filters=previous.filters,
        entity_text_hash=previous.entity_text_hash or record.entity_text_hash,
        embedding=previous.embedding or record.embedding,
    )


def _with_links(
    record: EntityRecord, new_id: str, linked_ids: list[str] | tuple[str, ...]
) -> EntityRecord:
    return EntityRecord(
        id=new_id,
        space_id=record.space_id,
        entity_text=record.entity_text,
        entity_type=record.entity_type,
        linked_memory_ids=tuple(linked_ids),
        filters=record.filters,
        entity_text_hash=record.entity_text_hash,
        embedding=record.embedding,
    )


def _expand(value: Any, shard: str) -> Any:
    if isinstance(value, str):
        return value.replace("{shard}", shard)
    if isinstance(value, dict):
        return {key: _expand(item, shard) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, shard) for item in value]
    return value


def _build_inline(spec: dict, shard: str, ctx: Any) -> EntityStore:
    target = spec.get("target")
    if not target:
        raise ValidationError("shard table template requires a 'target'")
    params = _expand(spec.get("params") or {}, shard)
    return EntityStoreProducer.build(str(target), params, ctx, name=f"shard_{shard}")


@EntityStoreProducer.register("shard_routed")
def _build(config):
    from jiuwen_memory.storage.shard_routing import ShardResolverProducer

    resolver = ShardResolverProducer.dep(config, "resolver")
    if not isinstance(resolver, ShardResolver):
        raise ValidationError("params.resolver must resolve to a ShardResolver")
    locator = LazyShardLocator(str(config.get("locator", "default")))
    mode = str(config.get("mode", "split"))
    if mode == "shared":
        inner = EntityStoreProducer.dep(config, "inner")
        return ShardRoutedEntityStore(
            resolver=resolver, locator=locator, mode=mode, inner=inner
        )
    template = config.get("table_template")
    overrides = config.get("tables") or {}
    if not isinstance(overrides, dict):
        raise ValidationError("params.tables must be a mapping of shard → store")
    stores: dict[str, EntityStore] = {}
    owned: list[BaseStore] = []
    try:
        for shard in resolver.shards:
            override = overrides.get(shard)
            if override is None:
                if not isinstance(template, dict):
                    raise ValidationError(
                        f"shard {shard!r} has neither table_template nor a tables override"
                    )
                store = _build_inline(template, shard, config.ctx)
                owned.append(store)
            elif isinstance(override, str):
                store = EntityStoreProducer.build_named(override, config.ctx)
            elif isinstance(override, dict):
                store = _build_inline(override, shard, config.ctx)
                owned.append(store)
            else:
                raise ValidationError(
                    f"params.tables[{shard!r}] must be a name or inline config"
                )
            stores[shard] = store
    except Exception:
        for store in owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        raise
    return ShardRoutedEntityStore(
        resolver=resolver,
        locator=locator,
        mode="split",
        stores=stores,
        owned=tuple(owned),
    )


def _require_all_succeeded(result: EntityBatchResult, action: str) -> None:
    """搬迁 / 清理的批量结果只要有失败条目就整体报错（不能当作成功继续切换归属）。"""
    failed = list(result.failed_ids or ())
    if failed:
        raise BackendError(f"entity {action} failed for records: {failed}")
