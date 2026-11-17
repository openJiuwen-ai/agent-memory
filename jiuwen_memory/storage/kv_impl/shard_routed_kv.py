# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``shard_routed`` KV 包装层与归属协议（F09 D7）。

**KV 是分片归属的唯一事实来源**。本包装层在 ``insert`` 时用 ``ShardResolver``
确定新记忆的分片，并把归属写进注入的 ``placement_store``（须声明
``ConditionalKV`` 能力）；其它派生存储经 :class:`PlacementLocator` 读取同一条
归属跟随，不自行按规则判定。

协议要点（完整推演见 F09 D7）：

1. 归属只能原子创建，同 id 同时至多一条；操作 token 区分「是不是我创建的」。
2. 数据只用原子创建写入，不覆写；晚到的旧 insert 不能覆盖新内容。
3. 删除先占位（``pending``/``active`` → ``cleaning``）、再删数据、最后按 token
   删归属；``cleaning`` 只由创建它的操作完成，不在线接管。
4. 超期 ``pending`` 由对账在线清理；遗留 ``cleaning`` 只在维护窗口清理。
5. ``update`` 不参与协调，与单表 KV 一样按 key 盲写。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from jiuwen_memory.common.errors import (
    ConflictError,
    NotFoundError,
    RelocationInProgressError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger, scope_for_log
from jiuwen_memory.common.type_def import (
    MEMORY_KEY_PREFIX,
    FilterExpr,
    Scope,
    memory_key,
)
from jiuwen_memory.storage.base import BaseStore, StoreType
from jiuwen_memory.storage.capability import StoreCapability, capabilities_of
from jiuwen_memory.storage.kv import (
    ConditionalKV,
    KvProducer,
    KVStore,
    load_memory_unit,
)
from jiuwen_memory.storage.security import CompositeStoreSecurity
from jiuwen_memory.storage.shard_routing import (
    SHARD_PLACEMENT_KEY_PREFIX,
    ShardLocation,
    ShardLocator,
    ShardResolver,
    ShardResolverProducer,
)
from jiuwen_memory.storage.shard_search import selected_shards
from jiuwen_memory.storage.types import KVMemoryListResult

from .memory_list import list_memory_entries
from .shard_admin import ReconcileReport, ShardAdmin, detect_retiring_shards
from .shard_placement import (
    PlacementRecord,
    PlacementStatus,
    TopologyEntry,
    placement_key,
)
from .shard_relocation import (
    DEFAULT_MAX_FAILURES,
    DEFAULT_MIN_AGE,
    DEFAULT_STALE_AFTER,
    RelocationReport,
    ShardRelocator,
    content_digest,
)

logger = get_logger(__name__)


def _is_memory_key(key: str) -> bool:
    return key.startswith(MEMORY_KEY_PREFIX)


def _require_conditional_kv(store: Any, role: str) -> None:
    """归属存储必须声明 ``ConditionalKV``；缺失时给出可执行的诊断。

    通用「能力缺失」无法区分「配错了后端」和「配错了实例」：``SQLite`` 等
    单写者后端没有实现条件 KV，配成归属存储只会在运行期才暴露。这里把支持的
    后端范围和已声明能力一并写进错误消息，装配期就能定位。
    """
    declared = capabilities_of(store)
    if StoreCapability.CONDITIONAL_KV in declared:
        return
    names = sorted(capability.value for capability in declared) or ["<none>"]
    raise UnsupportedStorageCapabilityError(
        f"{role} must declare {StoreCapability.CONDITIONAL_KV.value!r} "
        "(atomic create + compare-and-set + partial batch read). "
        f"Declared capabilities: {names}. "
        "PostgreSQL, Redis and the in-memory KV implement it; SQLite and other "
        "backends without conditional KV cannot be used as shard placement storage "
        "(single-node tests only)."
    )


def _memory_unit_id(key: str) -> str:
    return key[len(MEMORY_KEY_PREFIX):]


class PlacementLocator(ShardLocator):
    """只读归属记录的定位组件；批量部分读取，不缓存（F09 D6）。"""

    def __init__(self, placement: ConditionalKV) -> None:
        self._placement = placement

    def locator_name(self) -> str | None:
        return None  # KV 包装层自身就是定位提供方

    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        if not unit_ids:
            return ShardLocation()
        keys = [placement_key(unit_id) for unit_id in unit_ids]
        raw = self._placement.read_many(scope, keys)
        found: dict[str, str] = {}
        unlocated: set[str] = set()
        moving: set[str] = set()
        for unit_id, key in zip(unit_ids, keys, strict=True):
            value = raw.get(key)
            if value is None:
                unlocated.add(unit_id)
                continue
            try:
                record = PlacementRecord.from_bytes(value)
            except ValidationError:
                unlocated.add(unit_id)
                continue
            if record.status is PlacementStatus.ACTIVE:
                found[unit_id] = record.shard
            elif record.status is PlacementStatus.MOVING:
                found[unit_id] = record.shard
                moving.add(unit_id)
            else:
                unlocated.add(unit_id)
        return ShardLocation(
            found=found, unlocated=frozenset(unlocated), moving=frozenset(moving)
        )


class ShardRoutedKVStore(KVStore):
    """把 ``/memory/`` 记录路由到按分片拆分的物理 KV 表。"""

    def __init__(
        self,
        *,
        resolver: ShardResolver,
        passthrough: KVStore,
        placement: ConditionalKV,
        shard_stores: dict[str, KVStore],
        pending_timeout: float = 900.0,
        retiring: frozenset[str] = frozenset(),
        relocate_on_update: bool = False,
        stale_after: float = DEFAULT_STALE_AFTER,
        relocate_min_age: float = DEFAULT_MIN_AGE,
        relocate_max_failures: int = DEFAULT_MAX_FAILURES,
        owned: tuple[BaseStore, ...] = (),
    ) -> None:
        _require_conditional_kv(placement, "placement_store")
        missing = [name for name in resolver.shards if name not in shard_stores]
        if missing:
            raise ValidationError(
                f"shard_routed kv is missing physical stores for shards: {missing}"
            )
        unknown = [name for name in shard_stores if name not in resolver.shards]
        if unknown:
            raise ValidationError(
                f"shard_routed kv has stores for unconfigured shards: {unknown}"
            )
        self._resolver = resolver
        self._passthrough = passthrough
        self._placement = placement
        self._stores = dict(shard_stores)
        self._locator = PlacementLocator(placement)
        self._admin = ShardAdmin(
            placement=placement,
            passthrough=passthrough,
            stores=self._stores,
            resolver=resolver,
            locator=self._locator,
            pending_timeout=pending_timeout,
            retiring=retiring,
        )
        self._relocate_on_update = bool(relocate_on_update)
        self._relocatable_ports: tuple[Any, ...] = ()
        self._relocator = ShardRelocator(
            placement=placement,
            stores=self._stores,
            resolver=resolver,
            locator=self._locator,
            ports=lambda: self._relocatable_ports,
            is_retiring=self._admin.is_retiring,
            stale_after=stale_after,
            min_age=relocate_min_age,
            max_failures=relocate_max_failures,
        )
        self._owned = tuple(owned)

    # -- 自描述 --------------------------------------------------------------- #

    def store_type(self) -> StoreType:
        return StoreType.KV

    def capabilities(self) -> frozenset[StoreCapability]:
        return frozenset(
            {
                StoreCapability.SHARD_LOCATOR,
                StoreCapability.SHARD_ROUTED,
                StoreCapability.SHARD_PLACEMENT,
            }
        )

    @property
    def security(self):
        stores: list[BaseStore] = [self._passthrough, *self._stores.values()]
        if isinstance(self._placement, BaseStore):
            stores.append(self._placement)
        return CompositeStoreSecurity(stores)

    @property
    def shards(self) -> tuple[str, ...]:
        return tuple(self._stores)

    def health(self) -> None:
        self._passthrough.health()
        if isinstance(self._placement, BaseStore):
            self._placement.health()
        for shard in self._stores:
            self._stores[shard].health()

    def connection_dependencies(self) -> tuple[BaseStore, ...]:
        """汇总各物理分片表 + passthrough + 归属存储的连接预算（F09 D13）。

        这些实例多数是内联构建的，端口表看不到；装配期按对象身份去重后统一汇总。
        """
        deps: list[BaseStore] = [
            store for store in (self._passthrough, *self._stores.values())
            if isinstance(store, BaseStore)
        ]
        if isinstance(self._placement, BaseStore):
            deps.append(self._placement)
        return tuple(deps)

    def close(self) -> None:
        """关闭本层**自有**的内联实例；借用实例由注入方关闭。"""
        for store in self._owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        self._owned = ()

    # -- 定位（ShardLocator） ------------------------------------------------- #

    def locator_name(self) -> str | None:
        return None  # KV 包装层自身就是定位提供方

    def locate(self, scope: Scope, unit_ids: list[str]) -> ShardLocation:
        return self._locator.locate(scope, unit_ids)

    # -- 搬迁（F09 D16） ------------------------------------------------------ #

    def relocation_enabled(self) -> bool:
        """是否开启「改路由字段后自动搬迁」。"""
        return self._relocate_on_update

    def bind_relocatable_ports(self, ports: tuple[Any, ...]) -> None:
        """装配期绑定声明 ``SHARD_RELOCATABLE`` 的派生端口（F09 D16）。"""
        self._relocatable_ports = tuple(ports)

    def relocate_pending(
        self,
        *,
        scope: Scope | None = None,
        limit: int = 100,
        min_age: float | None = None,
        recover_stale: bool = False,
        stale_after: float | None = None,
    ) -> RelocationReport:
        """执行待搬迁记录；在线可周期调用（F09 D16 运维入口）。

        ``min_age`` 缺省取装配参数 ``relocate_min_age``（默认 30 秒）：标记不足该时长的
        记录本轮跳过，给 update 之后的派生写入与后端可见延迟留出时间。
        """
        if not self._relocate_on_update:
            raise ValidationError(
                "relocate_pending requires params.relocate_on_update=true"
            )
        return self._relocator.relocate_pending(
            scope=scope,
            limit=limit,
            min_age=min_age,
            recover_stale=recover_stale,
            stale_after=stale_after,
        )

    # -- CRUD ---------------------------------------------------------------- #

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._reject_control_key(key)
        if not _is_memory_key(key):
            self._passthrough.insert(scope, key, value, ttl)
            return
        unit = load_memory_unit(value, scope=scope, key=key)
        if unit is None:
            raise ValidationError(f"cannot route unreadable MemoryUnit at key {key!r}")
        unit_id = _memory_unit_id(key)
        shard = self._resolver.resolve(unit)
        self._admin.reject_if_retiring(shard)
        record = PlacementRecord(
            shard=shard,
            status=PlacementStatus.PENDING,
            token=str(uuid.uuid4()),
            created_at=self._placement.now(),
        )
        self._create_pending(scope, unit_id, record)
        try:
            self._write_data(scope, shard, key, value, ttl)
        except Exception:
            # ③ 失败分两种：分片中已有同 key 数据（冲突）或后端故障。前者按 D7 条件
            # 删除自己的 pending、残留数据留给对账；后者数据可能已写入也可能没有，
            # 同样尽力释放 pending，避免该 id 被占住到下次对账。两种情况下残留的
            # 无主数据都不可见，由对账清理。
            self._abort_pending(scope, unit_id, record)
            raise
        try:
            self._commit_active(scope, unit_id, record)
        except ConflictError as exc:
            raise ConflictError(
                "memory",
                unit_id,
                message=(
                    f"lost shard placement for memory {unit_id!r} during insert; "
                    "written data is orphaned and invisible until reconciliation"
                ),
            ) from exc

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        self._reject_control_key(key)
        if not _is_memory_key(key):
            self._passthrough.update(scope, key, value, ttl)
            return
        unit_id = _memory_unit_id(key)
        record = self._admin.read_record(scope, unit_id)
        if record is None:
            raise NotFoundError("memory", unit_id)
        if record.status is PlacementStatus.MOVING:
            raise RelocationInProgressError(unit_id)
        if record.status is not PlacementStatus.ACTIVE:
            raise NotFoundError("memory", unit_id)
        if not self._relocate_on_update:
            self._stores[record.shard].update(scope, key, value, ttl)
            return
        try:
            old_value = self._stores[record.shard].get(scope, key)
            self._stores[record.shard].update(scope, key, value, ttl)
        except NotFoundError:
            # 读归属之后该记忆已搬走并清理了源分片：不是「不存在」，而是该重试。
            if not _same_owner(record, self._admin.read_record(scope, unit_id)):
                raise RelocationInProgressError(unit_id) from None
            raise
        current = self._admin.read_record(scope, unit_id)
        if not _same_owner(record, current):
            # 读归属与写数据之间被开始搬迁（或已切换）：这次写入可能落在已复制过的
            # 源分片里。搬迁在切换前会复核源数据并回滚，这里让调用方重试，保证修改
            # 最终写进当前归属的分片。
            raise RelocationInProgressError(unit_id)
        self._mark_relocation(scope, unit_id, current, old_value, value)

    def _mark_relocation(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        old_value: bytes,
        new_value: bytes,
    ) -> None:
        """按更新前后的内容求值目标分片并写 ``relocate_to``（失败不影响 update）。

        条件替换失败说明归属刚被并发修改：重读一次，同一归属（只是标记变了）就按
        最新记录重试；归属已换主（开始搬迁 / 已切换）则让调用方重试整个 update。
        """
        old_unit = load_memory_unit(old_value, scope=scope, key=memory_key(unit_id))
        new_unit = load_memory_unit(new_value, scope=scope, key=memory_key(unit_id))
        if old_unit is None or new_unit is None:
            return
        try:
            target = self._resolver.resolve_on_update(old_unit, new_unit, record.shard)
        except ValidationError as exc:
            logger.warning("skip relocation marking for %s: %s", unit_id, exc)
            return
        if target is not None and self._admin.is_retiring(target):
            logger.warning(
                "skip relocation of %s: target shard %r is retiring", unit_id, target
            )
            target = None
        for _ in range(2):
            if self._relocator.mark(
                scope, unit_id, record, target, content_digest(new_value)
            ):
                return
            latest = self._admin.read_record(scope, unit_id)
            if latest is None or not _same_owner(record, latest):
                raise RelocationInProgressError(unit_id)
            record = latest
        logger.warning("relocation marking of %s kept losing the race", unit_id)

    def delete(self, scope: Scope, key: str) -> None:
        self._reject_control_key(key)
        if not _is_memory_key(key):
            self._passthrough.delete(scope, key)
            return
        unit_id = _memory_unit_id(key)
        for _ in range(3):
            record = self._admin.read_record(scope, unit_id)
            if record is None:
                return  # 幂等
            if record.status is PlacementStatus.MOVING or record.claim is not None:
                # 搬迁中或源分片清理已被认领（claim）期间不允许删除：否则删除完成后同 id
                # 重新插入，变慢的清理者恢复后会删掉新插入的数据（F09 D16）。
                raise RelocationInProgressError(unit_id)
            if record.status is PlacementStatus.CLEANING:
                # **不接管别人的 cleaning**（F09 D7 约束 4）。在线 delete 看到 cleaning 就
                # 视为删除已在进行并返回；若在这里生成新 token 条件替换，两个删除者会同时
                # 持有"清洁权"：后者的数据删除会晚于同 id 的新插入，删掉新数据。
                # 遗留 cleaning 只由维护窗口的独立路径（排空在途操作后）恢复。
                return
            cleaning = PlacementRecord(
                shard=record.shard,
                status=PlacementStatus.CLEANING,
                token=str(uuid.uuid4()),
                created_at=self._placement.now(),
            )
            if not self._admin.begin_cleaning(scope, unit_id, record, cleaning):
                if self._admin.is_being_cleaned(scope, unit_id):
                    return  # 别人的 cleaning：视为删除已在进行
                continue
            self._admin.delete_data(scope, record.shard, key)
            if record.cleanup_from is not None:
                self._cleanup_after_delete(scope, unit_id, record.cleanup_from)
            self._admin.finish_cleaning(scope, unit_id, cleaning)
            return
        raise ConflictError(
            "memory",
            unit_id,
            message=f"could not take cleaning ownership for memory {unit_id!r}",
        )

    def _cleanup_after_delete(self, scope: Scope, unit_id: str, shard: str) -> None:
        """删除时一并清理切换前留在源分片的副本（F09 D16）。"""
        store = self._stores.get(shard)
        if store is None:
            return
        try:
            store.delete(scope, memory_key(unit_id))
            for port in self._relocatable_ports:
                port.cleanup_unit(scope, unit_id, shard)
        except Exception:  # noqa: BLE001 - 清理失败保留归属，由对账 / 维护窗口处理
            logger.warning("delete cleanup of %s in shard %r failed", unit_id, shard)

    def get(self, scope: Scope, key: str) -> bytes:
        self._reject_control_key(key)
        if not _is_memory_key(key):
            return self._passthrough.get(scope, key)
        record = self._admin.read_record(scope, _memory_unit_id(key))
        if record is None or record.status not in (
            PlacementStatus.ACTIVE,
            PlacementStatus.MOVING,
        ):
            raise NotFoundError("memory", _memory_unit_id(key))
        return self._stores[record.shard].get(scope, key)

    def exists(self, scope: Scope, key: str) -> bool:
        self._reject_control_key(key)
        if not _is_memory_key(key):
            return self._passthrough.exists(scope, key)
        record = self._admin.read_record(scope, _memory_unit_id(key))
        if record is None or record.status not in (
            PlacementStatus.ACTIVE,
            PlacementStatus.MOVING,
        ):
            return False
        return self._stores[record.shard].exists(scope, key)

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        """批量点读，按**输入下标**一一对应返回；支持重复 key（S06 mget 契约）。

        定位按去重后的 unit_id 批量做一次，物理读取也按分片去重；回填必须回到原始
        下标——``{key: index}`` 这类映射会让重复 key 只剩最后一个位置（前面位置缺值）。
        """
        if not keys:
            return []
        for key in keys:
            self._reject_control_key(key)
        memory_positions = [
            (index, key) for index, key in enumerate(keys) if _is_memory_key(key)
        ]
        passthrough_positions = [
            (index, key) for index, key in enumerate(keys) if not _is_memory_key(key)
        ]
        result: dict[int, bytes] = {}
        if memory_positions:
            unit_ids = [_memory_unit_id(key) for _, key in memory_positions]
            location = self._locator.locate(scope, list(dict.fromkeys(unit_ids)))
            missing = [uid for uid in unit_ids if uid not in location.found]
            if missing:
                raise NotFoundError("memory", missing[0])
            grouped: dict[str, list[tuple[int, str]]] = {}
            for unit_id, (index, key) in zip(unit_ids, memory_positions, strict=True):
                grouped.setdefault(location.found[unit_id], []).append((index, key))
            for shard, items in grouped.items():
                unique_keys = list(dict.fromkeys(key for _, key in items))
                values = self._stores[shard].mget(scope, unique_keys)
                by_key = dict(zip(unique_keys, values, strict=True))
                for index, key in items:
                    result[index] = by_key[key]
        if passthrough_positions:
            values = self._passthrough.mget(scope, [key for _, key in passthrough_positions])
            for (index, _), value in zip(passthrough_positions, values, strict=True):
                result[index] = value
        return [result[index] for index in range(len(keys))]

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        out: dict[str, bytes] = {}
        covers_memory = (
            not prefix
            or prefix.startswith(MEMORY_KEY_PREFIX)
            or MEMORY_KEY_PREFIX.startswith(prefix)
        )
        covers_passthrough = not prefix.startswith(MEMORY_KEY_PREFIX)
        if covers_memory:
            inner_prefix = prefix if prefix.startswith(MEMORY_KEY_PREFIX) else MEMORY_KEY_PREFIX
            candidates: list[tuple[str, str, bytes]] = []
            for shard, store in self._stores.items():
                for key, value in store.scan(scope, inner_prefix):
                    candidates.append((shard, key, value))
            unit_ids = [_memory_unit_id(key) for _, key, _ in candidates]
            location = self._locator.locate(scope, unit_ids)
            for shard, key, value in candidates:
                unit_id = _memory_unit_id(key)
                if location.found.get(unit_id) == shard:
                    out.setdefault(key, value)
        if covers_passthrough:
            for key, value in self._passthrough.scan(scope, prefix):
                if key.startswith(SHARD_PLACEMENT_KEY_PREFIX) or _is_memory_key(key):
                    continue
                out.setdefault(key, value)
        return list(out.items())

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        selected = self._selected_shards(extensions)
        candidates: list[tuple[str, str, bytes]] = []
        for shard in selected:
            for key, value in self._stores[shard].scan(scope, MEMORY_KEY_PREFIX):
                candidates.append((shard, key, value))
        unit_ids = [_memory_unit_id(key) for _, key, _ in candidates]
        location = self._locator.locate(scope, unit_ids)
        entries: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for shard, key, value in candidates:
            unit_id = _memory_unit_id(key)
            if location.found.get(unit_id) != shard or key in seen:
                continue
            seen.add(key)
            entries.append((key, value))
        extra = dict(extensions or {})
        extra.pop("shards", None)
        return list_memory_entries(
            entries,
            scope,
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extra or None,
        )

    def scopes(self) -> list[Scope]:
        seen: dict[tuple[str, str, str, str, str], Scope] = {}

        def _key(scope: Scope) -> tuple[str, str, str, str, str]:
            return (scope.org, scope.space, scope.user, scope.agent, scope.session)

        for scope in self._passthrough.scopes():
            if self._passthrough_has_data(scope):
                seen[_key(scope)] = scope
        for store in self._stores.values():
            for scope in store.scopes():
                seen[_key(scope)] = scope
        return list(seen.values())

    # -- 拓扑登记与退役 -------------------------------------------------------- #

    def rebind_dependencies(self, replacements: dict[int, Any]) -> None:
        """加密保护后把被包装的依赖引用换成受保护实例（F09 D14）。

        包装层只持有引用；端口加密保护会为 passthrough / 借用的分片表创建新的
        加密包装实例。不重绑会让包装层继续写未保护的 raw 实例。
        """
        self._passthrough = replacements.get(id(self._passthrough), self._passthrough)
        if isinstance(self._placement, BaseStore):
            self._placement = replacements.get(id(self._placement), self._placement)
        for shard, store in list(self._stores.items()):
            self._stores[shard] = replacements.get(id(store), store)
        self._admin.rebind(
            passthrough=self._passthrough,
            placement=self._placement,
            stores=self._stores,
        )

    def shard_descriptors(self, kind: str = "kv") -> dict[str, dict[str, str]]:
        """每个分片的物理标识（供装配期自动拓扑登记）。"""
        out: dict[str, dict[str, str]] = {}
        for shard, store in self._stores.items():
            descriptor = store.resource_descriptor()
            if descriptor is not None:
                out[shard] = {kind: descriptor}
        return out

    # -- 拓扑 / 退役 / 对账 / 迁移（控制面在 :mod:`shard_admin`） ----------------- #

    def register_topology(self, descriptors: dict[str, dict[str, str]]) -> None:
        """启动期登记分片物理标识；不一致即拒绝启动。"""
        self._admin.register_topology(descriptors)

    def read_topology(self) -> dict[str, TopologyEntry]:
        """读取拓扑登记（分片 → 物理标识与状态）。"""
        return self._admin.read_topology()

    def retire_shard(self, shard: str) -> None:
        """把分片标记为 ``retiring``（退役第一步）。"""
        self._admin.retire_shard(shard)

    def shard_store(self, shard: str) -> KVStore:
        """返回某分片的物理 Store（离线迁移工具用；不发布为业务端口）。"""
        try:
            return self._stores[shard]
        except KeyError:
            raise ValidationError(f"unknown shard {shard!r}") from None

    def update_topology(self, shard: str, ports: dict[str, str]) -> None:
        """维护窗口内更新某分片的物理标识（只覆盖给出的端口键）。"""
        self._admin.update_topology(shard, ports)

    def repartition_unit(
        self, scope: Scope, unit_id: str, target_shard: str, unit: Any = None
    ) -> str:
        """把一条记忆迁到指定分片（维护窗口内重新分区用）。

        开启自动搬迁时复用同一个状态机（先复制 KV 与派生记录、再切换归属、最后清理），
        派生索引随 KV 一起搬，失败回滚且抛错，不会出现「KV 已搬、索引还在旧分片」。
        未开启时保持原行为：只搬 KV，之后须执行 ``--rebuild-indexes``。
        """
        if self._relocate_on_update:
            return self._relocator.relocate_unit(scope, unit_id, target_shard)
        return self._admin.repartition_unit(scope, unit_id, target_shard)

    def verify_retirement(self, shard: str) -> list[str]:
        """退役前校验：返回仍阻止退役的原因。"""
        return self._admin.verify_retirement(shard)

    def finish_retirement(self, shard: str) -> None:
        """退役最后一步：校验通过后删除该分片的拓扑登记。"""
        self._admin.finish_retirement(shard)

    def reconcile(
        self,
        *,
        scope: Scope | None = None,
        include_cleaning: bool = False,
        timeout: float | None = None,
    ) -> ReconcileReport:
        """在线清理超期 ``pending`` 与无主数据；``include_cleaning`` 仅供维护窗口。"""
        return self._admin.reconcile(
            scope=scope, include_cleaning=include_cleaning, timeout=timeout
        )

    # -- 插入协议的分步钩子（测试按需在此注入暂停 / 崩溃） ---------------------- #

    def _create_pending(self, scope: Scope, unit_id: str, record: PlacementRecord) -> None:
        try:
            self._placement.create_if_absent(scope, placement_key(unit_id), record.to_bytes())
        except ConflictError:
            raise ConflictError("memory", unit_id) from None

    def _write_data(
        self, scope: Scope, shard: str, key: str, value: bytes, ttl: float
    ) -> None:
        self._stores[shard].insert(scope, key, value, ttl)

    def _commit_active(self, scope: Scope, unit_id: str, record: PlacementRecord) -> None:
        active = PlacementRecord(
            shard=record.shard,
            status=PlacementStatus.ACTIVE,
            token=record.token,
            created_at=record.created_at,
        )
        self._placement.replace_if_value(
            scope, placement_key(unit_id), record.to_bytes(), active.to_bytes()
        )

    def _abort_pending(self, scope: Scope, unit_id: str, record: PlacementRecord) -> None:
        try:
            self._placement.delete_if_value(scope, placement_key(unit_id), record.to_bytes())
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始冲突
            logger.warning(
                "failed to release pending shard placement scope=%s unit_id=%s",
                scope_for_log(scope),
                unit_id,
            )

    # -- 内部 ---------------------------------------------------------------- #

    def _reject_control_key(self, key: str) -> None:
        if key.startswith(SHARD_PLACEMENT_KEY_PREFIX):
            raise ValidationError(f"key {key!r} uses the reserved shard placement prefix")

    def _selected_shards(self, extensions: dict[str, Any] | None) -> tuple[str, ...]:
        """``extensions["shards"]`` 的唯一解析实现（与向量 / 全文共用）。"""
        return selected_shards(extensions, self.shards)

    def _passthrough_has_data(self, scope: Scope) -> bool:
        for key, _ in self._passthrough.scan(scope):
            if not key.startswith(SHARD_PLACEMENT_KEY_PREFIX):
                return True
        return False


def _expand_shard(value: Any, shard: str) -> Any:
    """把配置模板里的 ``{shard}`` 展开成具体分片名（不递归字符串替换）。"""
    if isinstance(value, str):
        return value.replace("{shard}", shard)
    if isinstance(value, dict):
        return {key: _expand_shard(item, shard) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_shard(item, shard) for item in value]
    return value


def _protect_inline_kv(config: Any, store: KVStore) -> KVStore:
    """内联分片表按统一加密准入保护（F09 D13）；未启用时原样返回。"""
    from jiuwen_memory.storage.encryption import protect_inline

    protected = protect_inline(config, KvProducer, store, "kv")
    return protected


def _build_inline(spec: dict, shard: str, ctx: Any) -> KVStore:
    target = spec.get("target")
    if not target:
        raise ValidationError("shard table template requires a 'target'")
    params = _expand_shard(dict(spec.get("params") or {}), shard)
    return KvProducer.build(str(target), params, ctx, name=f"shard_{shard}")


def _build_conditional(config: Any, field_name: str) -> tuple[ConditionalKV, bool]:
    value = config.params.get(field_name)
    if value is None:
        raise ValidationError(f"shard_routed kv requires params.{field_name}")
    if isinstance(value, str):
        store = KvProducer.build_named(value, config.ctx)
        owned = False
    elif isinstance(value, dict):
        target = value.get("target")
        if not target:
            raise ValidationError(f"params.{field_name} inline config requires 'target'")
        store = KvProducer.build(str(target), dict(value.get("params") or {}), config.ctx)
        from jiuwen_memory.storage.encryption import protect_inline

        store = protect_inline(config, KvProducer, store, "kv")
        owned = True
    else:
        raise ValidationError(
            f"params.{field_name} must be a reference name or inline config"
        )
    _require_conditional_kv(store, f"params.{field_name}")
    return store, owned


@KvProducer.register("shard_routed")
def _build(config):
    """装配分片 KV：resolver + passthrough + placement + 每分片物理表。"""
    resolver = ShardResolverProducer.dep(config, "resolver")
    if not isinstance(resolver, ShardResolver):
        raise ValidationError("params.resolver must resolve to a ShardResolver")
    passthrough = KvProducer.dep(config, "passthrough")
    if isinstance(config.params.get("passthrough"), dict):
        # 内联 passthrough 不经过 protect_ports，构建期就按统一准入保护。
        from jiuwen_memory.storage.encryption import protect_inline

        passthrough = protect_inline(config, KvProducer, passthrough, "kv")
    placement, placement_owned = _build_conditional(config, "placement_store")

    template = config.get("table_template")
    overrides = config.get("tables") or {}
    if not isinstance(overrides, dict):
        raise ValidationError("params.tables must be a mapping of shard → store")
    stores: dict[str, KVStore] = {}
    owned: list[BaseStore] = []
    signatures: dict[str, str] = {}
    if placement_owned and isinstance(placement, BaseStore):
        owned.append(placement)
    try:
        for shard in resolver.shards:
            override = overrides.get(shard)
            if override is None:
                if not isinstance(template, dict):
                    raise ValidationError(
                        f"shard {shard!r} has neither table_template nor a tables override"
                    )
                store = _build_inline(template, shard, config.ctx)
                store = _protect_inline_kv(config, store)
                owned.append(store)
            elif isinstance(override, str):
                store = KvProducer.build_named(override, config.ctx)
            elif isinstance(override, dict):
                store = _build_inline(override, shard, config.ctx)
                store = _protect_inline_kv(config, store)
                owned.append(store)
            else:
                raise ValidationError(
                    f"params.tables[{shard!r}] must be a name or inline config"
                )
            if not isinstance(store, KVStore):
                raise ValidationError(f"shard {shard!r} store is not a KVStore")
            signature = json.dumps(
                _expand_shard(
                    override.get("params") if isinstance(override, dict) else template,
                    shard,
                ),
                sort_keys=True,
                default=str,
            )
            previous = signatures.get(signature)
            if previous is not None:
                raise ValidationError(
                    f"shards {previous!r} and {shard!r} expand to the same physical "
                    "store; split mode requires one independent resource per shard"
                )
            signatures[signature] = shard
            stores[shard] = store
    except Exception:
        # 装配失败只清理本次新建的内联实例；借用实例由注入方管理（F09 D13）。
        for store in owned:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        raise

    retiring = detect_retiring_shards(placement, resolver.shards)
    return ShardRoutedKVStore(
        resolver=resolver,
        passthrough=passthrough,
        placement=placement,
        shard_stores=stores,
        pending_timeout=float(config.get("pending_timeout", 900.0)),
        retiring=retiring,
        relocate_on_update=bool(config.get("relocate_on_update", False)),
        stale_after=float(config.get("relocate_stale_after", DEFAULT_STALE_AFTER)),
        relocate_min_age=float(config.get("relocate_min_age", DEFAULT_MIN_AGE)),
        relocate_max_failures=int(
            config.get("relocate_max_failures", DEFAULT_MAX_FAILURES)
        ),
        owned=tuple(owned),
    )


def _same_owner(before: PlacementRecord, after: PlacementRecord | None) -> bool:
    """两次读到的归属是否仍由同一次生效占有（只容许 ``relocate_to`` 标记变化）。"""
    return (
        after is not None
        and after.status is PlacementStatus.ACTIVE
        and after.token == before.token
        and after.shard == before.shard
    )
