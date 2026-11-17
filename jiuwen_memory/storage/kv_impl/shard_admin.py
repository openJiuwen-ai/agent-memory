# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""分片控制面：拓扑登记 / 退役、对账、重新分区（F09 D7 / D13）。

这些操作**不在读写热路径上**，只由启动流程与运维工具触发。与 :mod:`shard_routed_kv`
（读写协议）分开的原因：

- 拓扑登记与退役是部署治理，改的是控制记录，不参与记忆读写；
- 对账要扫全部分片与归属记录，属于批处理；
- 重新分区只在维护窗口内执行，是迁移工具，不应让数据面类承担它的状态机。

读写路径仍由 :class:`~storage.kv_impl.shard_routed_kv.ShardRoutedKVStore` 负责；它把
这里的公开方法原样转发，对外 API 不变。归属记录的原子条件操作仍只经
``ConditionalKV``，本模块不引入第二种写入方式。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger, scope_for_log
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, Scope, memory_key
from jiuwen_memory.storage.kv import ConditionalKV, KVStore
from jiuwen_memory.storage.shard_routing import (
    SHARD_PLACEMENT_KEY_PREFIX,
    ShardLocator,
    ShardResolver,
)

from .shard_placement import (
    CONTROL_SCOPE,
    PlacementRecord,
    PlacementStatus,
    TopologyEntry,
    TopologyStatus,
    is_control_suffix,
    placement_key,
    topology_key,
    unit_id_from_placement_key,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    """对账结果（只统计本进程实际处理/跳过的条数）。"""

    expired_pending: int = 0
    orphan_data: int = 0
    leftover_cleaning: int = 0
    skipped: int = 0


def read_topology_best_effort(
    placement: ConditionalKV, shards: tuple[str, ...]
) -> dict[str, TopologyEntry]:
    """构造期只读一次拓扑，用于识别 ``retiring`` 分片。

    登记与一致性校验由启动流程显式调用 :meth:`ShardAdmin.register_topology`；
    这里读失败不应阻止构造（后端不可用交给 health 报告）。
    """
    try:
        raw = placement.read_many(CONTROL_SCOPE, [topology_key(shard) for shard in shards])
    except Exception:  # noqa: BLE001 - 后端不可用应由后续健康检查报告
        return {}
    entries: dict[str, TopologyEntry] = {}
    for shard in shards:
        value = raw.get(topology_key(shard))
        if value is None:
            continue
        try:
            parsed = TopologyEntry.from_bytes(value)
        except ValidationError:
            continue
        entries[shard] = TopologyEntry(shard=shard, ports=parsed.ports, status=parsed.status)
    return entries


def detect_retiring_shards(
    placement: ConditionalKV, shards: tuple[str, ...]
) -> frozenset[str]:
    """构造期识别 ``retiring`` 分片：这些分片拒绝新记忆，读取照常。"""
    return frozenset(
        name
        for name, entry in read_topology_best_effort(placement, shards).items()
        if entry.status is TopologyStatus.RETIRING
    )


def _legacy_milvus_topology_compatible(
    registered: dict[str, str], configured: dict[str, str]
) -> bool:
    """兼容旧版 Milvus descriptor（仅实例级，缺少 collection）。

    只接受 ``vector:*`` 键上 ``registered == "milvus://host"`` 且配置值是在同一
    实例下新增了 collection 的情况；collection 本身变化仍按物理身份变更拒绝。
    """
    if set(registered) != set(configured):
        return False
    for key, old in registered.items():
        new = configured[key]
        if old == new:
            continue
        if (
            key.startswith("vector:")
            and old.startswith("milvus://")
            and new.startswith(old.rstrip("/") + "/")
        ):
            continue
        return False
    return True


class ShardAdmin:
    """分片控制面：拓扑、退役、对账与重新分区。"""

    def __init__(
        self,
        *,
        placement: ConditionalKV,
        passthrough: KVStore,
        stores: dict[str, KVStore],
        resolver: ShardResolver,
        locator: ShardLocator,
        pending_timeout: float = 900.0,
        retiring: frozenset[str] = frozenset(),
    ) -> None:
        self._placement = placement
        self._passthrough = passthrough
        self._stores = stores
        self._resolver = resolver
        self._locator = locator
        self._pending_timeout = float(pending_timeout)
        self._retiring = frozenset(retiring)

    # -- 与数据面共享的归属记录原子操作 ---------------------------------------- #

    def read_record(self, scope: Scope, unit_id: str) -> PlacementRecord | None:
        """读取归属记录；不存在或结构损坏返回 ``None``。"""
        key = placement_key(unit_id)
        raw = self._placement.read_many(scope, [key]).get(key)
        if raw is None:
            return None
        try:
            return PlacementRecord.from_bytes(raw)
        except ValidationError:
            logger.warning(
                "corrupt shard placement record scope=%s unit_id=%s",
                scope_for_log(scope),
                unit_id,
            )
            return None

    def begin_cleaning(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        cleaning: PlacementRecord,
    ) -> bool:
        """按整值条件把归属改为 ``cleaning``；失败返回 ``False``（调用方重读）。"""
        try:
            self._placement.replace_if_value(
                scope, placement_key(unit_id), record.to_bytes(), cleaning.to_bytes()
            )
        except ConflictError:
            return False
        return True

    def is_being_cleaned(self, scope: Scope, unit_id: str) -> bool:
        current = self.read_record(scope, unit_id)
        return current is not None and current.status is PlacementStatus.CLEANING

    def finish_cleaning(
        self, scope: Scope, unit_id: str, cleaning: PlacementRecord
    ) -> None:
        self._placement.delete_if_value(
            scope, placement_key(unit_id), cleaning.to_bytes()
        )

    def delete_data(self, scope: Scope, shard: str, key: str) -> None:
        self._stores[shard].delete(scope, key)

    # -- 退役状态 -------------------------------------------------------------- #

    def is_retiring(self, shard: str) -> bool:
        return shard in self._retiring

    def reject_if_retiring(self, shard: str) -> None:
        if self.is_retiring(shard):
            raise ValidationError(
                f"shard {shard!r} is retiring and rejects new memories"
            )

    # -- 拓扑登记 -------------------------------------------------------------- #

    def register_topology(self, descriptors: dict[str, dict[str, str]]) -> None:
        """启动期登记分片物理标识；已存在则比对，不一致即拒绝启动。"""
        for shard in self._resolver.shards:
            if shard not in descriptors:
                raise ValidationError(f"shard {shard!r} has no physical descriptor")
        existing = self.read_topology()
        for shard, ports in descriptors.items():
            entry = TopologyEntry(shard=shard, ports=dict(ports))
            previous = existing.get(shard)
            if previous is None:
                self._placement.create_if_absent(
                    CONTROL_SCOPE, topology_key(shard), entry.to_bytes()
                )
                continue
            if previous.ports != entry.ports:
                if _legacy_milvus_topology_compatible(previous.ports, entry.ports):
                    # Milvus descriptor 从 instance 级升级到 collection 级；旧登记
                    # 没有 collection，允许一次兼容并原地升级，避免升级即启动失败。
                    logger.warning(
                        "upgrading legacy Milvus topology descriptor for shard %r",
                        shard,
                    )
                    # 滚动升级时多个实例会读到同一旧值；后写入者可能因条件替换
                    # 失败收到 ConflictError。重读一次，若已被其它实例升级到目标
                    # 值则视为成功，不能因此让本实例启动失败。
                    for attempt in range(2):
                        try:
                            self.update_topology(shard, entry.ports)
                            break
                        except ConflictError:
                            latest = self.read_topology().get(shard)
                            if latest is not None and latest.ports == entry.ports:
                                break
                            if attempt == 1:
                                raise
                    continue
                raise ValidationError(
                    f"shard {shard!r} physical identity changed: registered "
                    f"{previous.ports} vs configured {entry.ports}; "
                    "use the relocate/retire tooling instead of editing config"
                )
        for shard in existing:
            if shard not in self._resolver.shards:
                raise ValidationError(
                    f"registered shard {shard!r} is absent from configuration; "
                    "retire it before removing it from shards"
                )

    def read_topology(self) -> dict[str, TopologyEntry]:
        prefix = f"{SHARD_PLACEMENT_KEY_PREFIX}topology/"
        entries: dict[str, TopologyEntry] = {}
        for key, value in self._placement.scan_entries(CONTROL_SCOPE, prefix):
            shard = key[len(prefix):]
            if not shard:
                continue
            entry = TopologyEntry.from_bytes(value)
            entries[shard] = TopologyEntry(
                shard=shard, ports=entry.ports, status=entry.status
            )
        return entries

    def update_topology(self, shard: str, ports: dict[str, str]) -> None:
        """维护窗口内更新某分片的物理标识（迁移完成后调用）。

        **只覆盖给出的端口键，其余登记原样保留**：``--relocate-shard`` 只搬一个端口
        的物理资源，若整体替换登记，向量 / 全文 / 实体层的标识会被抹掉，下一次启动
        的物理身份比对就会把这次迁移判成「未经迁移的物理变更」而拒绝启动。
        短键（``kv=...``）按能力前缀唯一匹配到 ``{capability}:{port_name}`` 登记键。
        """
        entry = self.read_topology().get(shard)
        if entry is None:
            raise ValidationError(f"shard {shard!r} is not registered")
        merged = merge_topology_ports(entry.ports, ports)
        updated = TopologyEntry(shard=shard, ports=merged, status=entry.status)
        self._placement.replace_if_value(
            CONTROL_SCOPE, topology_key(shard), entry.to_bytes(), updated.to_bytes()
        )

    def retire_shard(self, shard: str) -> None:
        """把分片标记为 ``retiring``（退役第一步）。"""
        if shard not in self._stores:
            raise ValidationError(f"unknown shard {shard!r}")
        entry = self.read_topology().get(shard)
        if entry is None:
            raise ValidationError(f"shard {shard!r} is not registered")
        updated = TopologyEntry(
            shard=shard, ports=entry.ports, status=TopologyStatus.RETIRING
        )
        try:
            self._placement.replace_if_value(
                CONTROL_SCOPE, topology_key(shard), entry.to_bytes(), updated.to_bytes()
            )
        except ConflictError:
            raise ConflictError(
                "shard_topology", shard, message="concurrent topology change"
            ) from None
        self._retiring = self._retiring | {shard}

    def verify_retirement(self, shard: str) -> list[str]:
        """退役前校验：返回仍阻止退役的原因；空列表表示可以退役。"""
        reasons: list[str] = []
        for scope in self._reconcile_scopes():
            for key, raw in self._placement.scan_entries(scope, SHARD_PLACEMENT_KEY_PREFIX):
                unit_id = unit_id_from_placement_key(key)
                if unit_id is None or is_control_suffix(unit_id):
                    continue
                try:
                    record = PlacementRecord.from_bytes(raw)
                except ValidationError:
                    continue
                if record.shard == shard:
                    reasons.append(
                        f"placement still points to {shard!r} in scope {scope_for_log(scope)}"
                    )
                    break
            if reasons:
                break
        store = self._stores.get(shard)
        if store is not None:
            for scope in store.scopes():
                if store.scan(scope, MEMORY_KEY_PREFIX):
                    reasons.append(f"shard {shard!r} still holds memory data")
                    break
        return reasons

    def finish_retirement(self, shard: str) -> None:
        """退役最后一步：校验通过后删除该分片的拓扑登记（维护窗口内）。"""
        if shard in self._resolver.shards:
            raise ValidationError(
                f"shard {shard!r} is still configured; remove it from shard_resolver first"
            )
        entry = self.read_topology().get(shard)
        if entry is None:
            return
        self._placement.delete_if_value(
            CONTROL_SCOPE, topology_key(shard), entry.to_bytes()
        )

    # -- 重新分区（维护窗口） -------------------------------------------------- #

    def repartition_unit(self, scope: Scope, unit_id: str, target_shard: str) -> str:
        """把一条记忆迁到指定分片（维护窗口内重新分区用）。

        顺序：读旧数据 → 在目标分片原子创建 → CAS 归属 → 删除旧分片数据。
        目标写入失败时旧数据与归属保持不变；CAS 失败时删除刚写入的目标数据。
        返回最终分片。派生索引需由调用方另行重建（见迁移脚本说明）。
        """
        if target_shard not in self._stores:
            raise ValidationError(f"unknown target shard {target_shard!r}")
        self.reject_if_retiring(target_shard)
        record = self.read_record(scope, unit_id)
        if record is None or record.status is not PlacementStatus.ACTIVE:
            raise NotFoundError("memory", unit_id)
        if record.shard == target_shard:
            return target_shard
        key = memory_key(unit_id)
        source = self._stores[record.shard]
        raw = source.get(scope, key)  # 缺失即 NotFoundError
        ttl = source.remaining_ttl(scope, key)
        if ttl is None:
            # ``None`` = 源记录在读取后过期/消失（内置后端都实现了 remaining_ttl）。
            # 这是读取与迁移之间的状态变化，按冲突处理：不能按 0 写入（会变成永久副本），
            # 也不能报成 NotFoundError（读数据已成功）。旧数据与归属保持不变，可跳过或重试。
            raise ConflictError(
                "memory",
                unit_id,
                message=(
                    f"cannot repartition memory {unit_id!r}: source row expired or "
                    f"disappeared after read, or {type(source).__name__} cannot report "
                    "remaining TTL"
                ),
            )
        target = self._stores[target_shard]
        target.insert(scope, key, raw, ttl=ttl)  # 已存在则冲突中止，旧数据仍在
        updated = PlacementRecord(
            shard=target_shard,
            status=PlacementStatus.ACTIVE,
            token=str(uuid.uuid4()),
            created_at=self._placement.now(),
        )
        try:
            self._placement.replace_if_value(
                scope, placement_key(unit_id), record.to_bytes(), updated.to_bytes()
            )
        except ConflictError:
            target.delete(scope, key)
            raise
        try:
            source.delete(scope, key)
        except Exception:  # noqa: BLE001
            # 归属已切到目标分片，源分片里的旧数据成为「错放副本」（归属指向别处）。
            # 对账不清理这类副本（见 _reconcile_orphans），需在维护窗口人工处理。
            logger.warning(
                "repartition left a misplaced copy in the source shard after placement "
                "switch (not reclaimed by reconcile; clean up manually) scope=%s unit_id=%s",
                scope_for_log(scope),
                unit_id,
            )
        return target_shard

    # -- 对账（只在明确调用时执行） -------------------------------------------- #

    def reconcile(
        self,
        *,
        scope: Scope | None = None,
        include_cleaning: bool = False,
        timeout: float | None = None,
    ) -> ReconcileReport:
        """在线清理超期 ``pending`` 与无主数据；``include_cleaning`` 仅供维护窗口。"""
        limit = self._pending_timeout if timeout is None else float(timeout)
        now = self._placement.now()
        expired = orphan = leftover_cleaning = skipped = 0
        scopes = [scope] if scope is not None else self._reconcile_scopes()
        for current in scopes:
            cleaned, leftover = self._reconcile_placements(
                current, now, limit, include_cleaning
            )
            expired += cleaned
            leftover_cleaning += leftover
            cleaned, not_owned = self._reconcile_orphans(current)
            orphan += cleaned
            skipped += not_owned
        return ReconcileReport(
            expired_pending=expired,
            orphan_data=orphan,
            leftover_cleaning=leftover_cleaning,
            skipped=skipped,
        )

    def _reconcile_scopes(self) -> list[Scope]:
        scopes: dict[tuple[str, str, str, str, str], Scope] = {}
        for source in (self._placement, self._passthrough, *self._stores.values()):
            scopes_fn = getattr(source, "scopes", None)
            if not callable(scopes_fn):
                continue
            for scope in scopes_fn():
                scopes[(scope.org, scope.space, scope.user, scope.agent, scope.session)] = scope
        return list(scopes.values())

    def _reconcile_placements(
        self, scope: Scope, now: float, limit: float, include_cleaning: bool
    ) -> tuple[int, int]:
        cleaned = 0
        leftover = 0
        for key, raw in self._placement.scan_entries(scope, SHARD_PLACEMENT_KEY_PREFIX):
            unit_id = unit_id_from_placement_key(key)
            if unit_id is None or is_control_suffix(unit_id):
                continue
            try:
                record = PlacementRecord.from_bytes(raw)
            except ValidationError:
                continue
            if record.status is PlacementStatus.CLEANING and not include_cleaning:
                leftover += 1
                continue
            if record.status is PlacementStatus.PENDING and now - record.created_at > limit:
                if self._take_over(scope, unit_id, record, now):
                    cleaned += 1
            elif record.status is PlacementStatus.CLEANING and include_cleaning:
                if self._take_over(scope, unit_id, record, now):
                    cleaned += 1
        return cleaned, leftover

    def _take_over(
        self, scope: Scope, unit_id: str, record: PlacementRecord, now: float
    ) -> bool:
        """把归属改为自己的 ``cleaning`` 后删数据、删归属（占位期间独占该 id）。"""
        cleaning = PlacementRecord(
            shard=record.shard,
            status=PlacementStatus.CLEANING,
            token=str(uuid.uuid4()),
            created_at=now,
        )
        if not self.begin_cleaning(scope, unit_id, record, cleaning):
            return False
        self.delete_data(scope, record.shard, memory_key(unit_id))
        self.finish_cleaning(scope, unit_id, cleaning)
        return True

    def _reconcile_orphans(self, scope: Scope) -> tuple[int, int]:
        cleaned = 0
        skipped = 0
        for shard, store in self._stores.items():
            keys = [key for key, _ in store.scan(scope, MEMORY_KEY_PREFIX)]
            if not keys:
                continue
            location = self._locator.locate(
                scope, [_memory_unit_id(key) for key in keys]
            )
            for key in keys:
                unit_id = _memory_unit_id(key)
                if location.found.get(unit_id) == shard:
                    continue
                if unit_id in location.found:
                    # 归属在别的分片：数据是错放副本。**对账不清理它**——在线删除会与
                    # 并发的重分区/重建竞争（查与删之间同 id 可能刚被迁走或重建）。
                    # 这类副本需要在排空在途操作后人工处理（D8 / 已知遗留）。
                    continue
                cleaning = PlacementRecord(
                    shard=shard,
                    status=PlacementStatus.CLEANING,
                    token=str(uuid.uuid4()),
                    created_at=self._placement.now(),
                )
                try:
                    self._placement.create_if_absent(
                        scope, placement_key(unit_id), cleaning.to_bytes()
                    )
                except ConflictError:
                    skipped += 1
                    continue
                self.delete_data(scope, shard, key)
                self.finish_cleaning(scope, unit_id, cleaning)
                cleaned += 1
        return cleaned, skipped

    # -- 依赖重绑 -------------------------------------------------------------- #

    def rebind(
        self,
        *,
        passthrough: KVStore,
        placement: Any,
        stores: dict[str, KVStore],
    ) -> None:
        """加密保护后把物理依赖换成受保护实例（与数据面共用同一批引用）。"""
        self._passthrough = passthrough
        self._placement = placement
        self._stores = stores


def merge_topology_ports(
    registered: dict[str, str], updates: dict[str, str]
) -> dict[str, str]:
    """把 ``updates`` 合并进已登记端口表（短键按能力唯一展开）。"""
    merged = dict(registered)
    for key, identity in updates.items():
        if not isinstance(key, str) or not key:
            raise ValidationError(f"topology port key must be a non-empty string: {key!r}")
        if not isinstance(identity, str) or not identity:
            raise ValidationError(
                f"topology identity for port {key!r} must be a non-empty string"
            )
        if key in merged:
            merged[key] = identity
            continue
        candidates = [name for name in merged if name.split(":", 1)[0] == key]
        if len(candidates) != 1:
            raise ValidationError(
                f"topology port {key!r} is ambiguous or not registered for this shard; "
                f"registered ports: {sorted(merged)}"
            )
        merged[candidates[0]] = identity
    return merged


def _memory_unit_id(key: str) -> str:
    return key[len(MEMORY_KEY_PREFIX):]
