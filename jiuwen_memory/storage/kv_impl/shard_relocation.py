# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""改路由字段后的自动搬迁状态机（F09 D16）。

``update`` 只在归属记录上打 ``relocate_to`` 标记并登记待搬迁索引；真正的搬迁由
:meth:`ShardRelocator.relocate_pending`（运维 / 定时任务）执行，维护窗口内的重新分区
经 :meth:`ShardRelocator.relocate_unit` 复用同一状态机。状态机与顺序：

.. code-block:: text

    active(S, relocate_to=T) --CAS--> moving(S→T, step=copy_kv)
      --copy_kv--> step=copy_derived --各端口 relocate_unit--> step=switch
      --源数据未变才 CAS--> active(T, cleanup_from=S, claim=c)
      --持有 claim：各端口 cleanup_unit(S) + 删源 KV + 仅持有者 CAS--> active(T)

开始搬迁后先比对 ``mark_digest`` 与源分片当前正文：不一致说明标记出自一个已被覆盖的写入，
在持有 ``moving`` 的情况下按当前真源重新裁决目标。清理阶段持有 ``claim`` 独占执行权，
``claim`` 存在期间 delete 与新的搬迁都不能开始。

每一步开始前持久化 ``step``；切换前失败一律回滚到 ``active(S, relocate_to=T)``（连续失败
超过上限则清除标记）。在线执行不接管别人遗留的 ``moving``；``recover_stale=True`` 只在
维护窗口、排空在途操作后使用。

**待搬迁索引**：每条有搬迁工作的记忆在归属存储中另有一条
``/__shard_placement/relocation/<unit_id>``，``relocate_pending`` 只扫描这些条目，成本与
待处理数量成正比而不是与全部记忆成正比。索引只是"去哪找"的提示，权威状态始终在
归属记录里：先写索引、再写标记；索引指向的记录已无工作且索引已超过 ``stale_after``
时才删除，并按读到的值条件删除，不会误删刚被新标记刷新的索引。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, cast

from jiuwen_memory.common.errors import ConflictError, NotFoundError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.storage.kv import ConditionalKV, KVStore, load_memory_unit
from jiuwen_memory.storage.shard_routing import ShardLocator, ShardResolver

from .shard_placement import (
    RELOCATION_INDEX_PREFIX,
    PlacementRecord,
    PlacementStatus,
    placement_key,
    relocation_index_key,
)

logger = get_logger(__name__)

_STEP_COPY_KV = "copy_kv"
_STEP_COPY_DERIVED = "copy_derived"
_STEP_SWITCH = "switch"
_STEP_ROLLBACK = "rollback"
_STEPS_BEFORE_SWITCH = frozenset({_STEP_COPY_KV, _STEP_COPY_DERIVED})

#: 未配置时的默认值：``min_age`` 要大于派生后端的可见延迟（ES 刷新、Milvus 一致性），
#: 并给 update 之后的派生写入留出落地时间；``max_failures`` 之后放弃该条标记。
DEFAULT_MIN_AGE = 30.0
DEFAULT_STALE_AFTER = 900.0
DEFAULT_MAX_FAILURES = 5


class _SourceChanged(Exception):
    """复制之后源分片的数据又被改写：本次搬迁作废，回滚后按新内容重来。"""


class _SwitchUnsafe(Exception):
    """切换前复核发现目标副本不存在或与源不一致：必须回滚，绝不切换。

    ``step=switch`` 在 CAS 之前持久化；若随后回滚删掉了目标副本、而派生清理失败，
    归属会停在 ``moving(step=switch)``。维护恢复不能只看 step 就前滚，否则会把归属
    切到不存在的目标并删除唯一的源数据。
    """


@dataclass(frozen=True)
class RelocationReport:
    """一次 ``relocate_pending`` 的结果。"""

    moved: int = 0
    skipped: int = 0
    failed: int = 0
    stale_moving: int = 0
    abandoned: int = 0

    def __str__(self) -> str:
        return (
            f"moved={self.moved} skipped={self.skipped} failed={self.failed} "
            f"stale_moving={self.stale_moving} abandoned={self.abandoned}"
        )


class ShardRelocator:
    """搬迁执行器：只通过 ``ConditionalKV`` 改归属，经派生端口能力复制 / 清理记录。"""

    def __init__(
        self,
        *,
        placement: ConditionalKV,
        stores: dict[str, KVStore],
        resolver: ShardResolver,
        locator: ShardLocator,
        ports: Callable[[], Iterable[Any]],
        is_retiring: Callable[[str], bool],
        stale_after: float = DEFAULT_STALE_AFTER,
        min_age: float = DEFAULT_MIN_AGE,
        max_failures: int = DEFAULT_MAX_FAILURES,
    ) -> None:
        if max_failures <= 0:
            raise ValidationError("relocate_max_failures must be a positive integer")
        self._placement = placement
        self._stores = stores
        self._resolver = resolver
        self._locator = locator
        self._ports = ports
        self._is_retiring = is_retiring
        self._stale_after = float(stale_after)
        self._min_age = float(min_age)
        self._max_failures = int(max_failures)

    # -- 标记（供 update 调用） -------------------------------------------------- #

    def mark(
        self,
        scope: Scope,
        unit_id: str,
        expected: PlacementRecord,
        target: str | None,
        digest: str | None = None,
    ) -> bool:
        """把 ``expected`` 条件替换为带 ``relocate_to=target`` 的记录。

        ``digest`` 是本次写入正文的摘要，执行搬迁时用来判断这个标记是否仍对应源分片
        的当前数据。只改搬迁意图相关字段，清理中的 ``cleanup_from`` / ``claim`` 原样保留。
        先登记待搬迁索引再改标记：两步之间崩溃只会留下多余的索引（之后清理），不会
        留下没有索引、永远不被处理的标记。返回是否写入成功；标记未变化视为成功。
        """
        wanted_digest = digest if target is not None else None
        if expected.relocate_to == target and expected.mark_digest == wanted_digest:
            return True
        if target is None and expected.relocate_to is not None:
            # 取消搬迁同样要绑定数据版本：只有本请求写入的正文仍是源分片当前内容
            # 时才允许清除标记。一个更早的请求（它的正文已被后来的写入覆盖）不能在
            # CAS 重试时清掉最新写入建立的搬迁意图。
            if digest is None:
                return True
            try:
                current = self._stores[expected.shard].get(
                    scope, memory_key(unit_id)
                )
            except Exception:  # noqa: BLE001 - 读不到就不冒险取消
                return True
            if content_digest(current) != digest:
                logger.warning(
                    "ignore stale relocation cancel for %s: source data was "
                    "overwritten by a later write",
                    unit_id,
                )
                return True
        if target is not None or expected.cleanup_from is not None:
            if not self._touch_index(scope, unit_id):
                return False
        marked = replace(
            expected,
            created_at=self._placement.now(),
            relocate_to=target,
            mark_digest=wanted_digest,
        )
        return self._cas(scope, unit_id, expected, marked)

    # -- 入口 ------------------------------------------------------------------ #

    def relocate_pending(
        self,
        *,
        scope: Scope | None = None,
        limit: int = 100,
        min_age: float | None = None,
        recover_stale: bool = False,
        stale_after: float | None = None,
    ) -> RelocationReport:
        """处理待搬迁 / 已切换待清理 / 遗留 ``moving`` 的记录（只扫描待搬迁索引）。"""
        if limit <= 0:
            return RelocationReport()
        age = self._min_age if min_age is None else float(min_age)
        stale_limit = self._stale_after if stale_after is None else float(stale_after)
        now = self._placement.now()
        moved = skipped = failed = stale = abandoned = 0
        processed = 0
        scopes = [scope] if scope is not None else self._scopes()
        for current in scopes:
            if processed >= limit:
                break
            for unit_id, index_value, record in self._pending(current):
                if processed >= limit:
                    break
                if record is None or not _has_work(record):
                    self._drop_stale_index(current, unit_id, index_value, now, stale_limit)
                    continue
                if record.status is PlacementStatus.ACTIVE and record.cleanup_from is not None:
                    # 清理未结束前不开始新的搬迁：源分片可能仍被别的执行者删除中。
                    if record.claim is not None and not self._claim_stale(
                        record, now, stale_limit
                    ):
                        skipped += 1
                        continue
                    if record.claim is not None:
                        stale += 1
                        if not recover_stale:
                            continue
                    processed += 1
                    if self._cleanup(
                        current,
                        unit_id,
                        record,
                        index_value,
                        take_over=record.claim is not None,
                    ):
                        moved += 1
                    else:
                        failed += 1
                    continue
                if record.status is PlacementStatus.ACTIVE:
                    if now - record.created_at < age:
                        skipped += 1
                        continue
                    processed += 1
                    outcome = self._relocate_one(current, unit_id, record, now, index_value)
                    if outcome == "moved":
                        moved += 1
                    elif outcome == "abandoned":
                        abandoned += 1
                    elif outcome == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                    continue
                if record.status is PlacementStatus.MOVING:
                    if now - record.created_at <= stale_limit:
                        skipped += 1
                        continue
                    stale += 1
                    if not recover_stale:
                        continue
                    processed += 1
                    if self._recover_one(current, unit_id, record, index_value):
                        moved += 1
                    else:
                        failed += 1
        return RelocationReport(
            moved=moved,
            skipped=skipped,
            failed=failed,
            stale_moving=stale,
            abandoned=abandoned,
        )

    def relocate_unit(self, scope: Scope, unit_id: str, target: str) -> str:
        """维护窗口内把一条记忆搬到 ``target``（重新分区复用同一状态机）。

        返回最终分片；没有完成搬迁时抛错，归属保持在源分片（可重跑）。
        """
        if target not in self._stores:
            raise ValidationError(f"unknown target shard {target!r}")
        if self._is_retiring(target):
            raise ValidationError(f"shard {target!r} is retiring and accepts no new memory")
        index_value = self._read_index(scope, unit_id)
        record = self._read(scope, unit_id)
        if record is None or record.status is not PlacementStatus.ACTIVE:
            raise NotFoundError("memory", unit_id)
        if record.cleanup_from is not None:
            if record.claim is not None:
                raise ConflictError(
                    "memory_relocation", unit_id, message="previous cleanup still claimed"
                )
            if not self._cleanup(scope, unit_id, record, index_value):
                raise ConflictError(
                    "memory_relocation", unit_id, message="previous relocation cleanup failed"
                )
            record = self._read(scope, unit_id)
            if record is None or record.status is not PlacementStatus.ACTIVE:
                raise NotFoundError("memory", unit_id)
        if record.shard == target:
            return target
        raw = self._stores[record.shard].get(scope, memory_key(unit_id))
        if not self.mark(scope, unit_id, record, target, content_digest(raw)):
            raise ConflictError("memory_relocation", unit_id, message="placement changed")
        index_value = self._read_index(scope, unit_id)
        marked = self._read(scope, unit_id)
        if marked is None or marked.relocate_to != target:
            raise ConflictError("memory_relocation", unit_id, message="placement changed")
        outcome = self._relocate_one(
            scope, unit_id, marked, self._placement.now(), index_value
        )
        if outcome != "moved":
            raise ConflictError(
                "memory_relocation",
                unit_id,
                message=f"relocation to {target!r} did not complete ({outcome})",
            )
        return target

    # -- 单条流程 -------------------------------------------------------------- #

    def _relocate_one(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        now: float,
        index_value: bytes | None,
    ) -> str:
        target = record.relocate_to
        if target is None or target not in self._stores or target == record.shard:
            return "failed"
        if self._is_retiring(target):
            logger.warning(
                "abandon relocation of %s: target shard %r is retiring", unit_id, target
            )
            self._requeue(scope, unit_id, record, None, None, index_value)
            return "abandoned"
        moving = replace(
            record,
            status=PlacementStatus.MOVING,
            token=str(uuid.uuid4()),
            created_at=now,
            step=_STEP_COPY_KV,
        )
        if not self._cas(scope, unit_id, record, moving):
            return "skipped"  # 并发 update / delete 或另一个执行者已接手
        try:
            raw = self._stores[record.shard].get(scope, memory_key(unit_id))
            if record.mark_digest is not None and content_digest(raw) != record.mark_digest:
                # 标记对应的写入已被后来的写入覆盖：按源分片当前数据（我们持有 moving，
                # 期间不会再有写入）重新裁决，而不是信任最后完成打标的那个请求。
                fresh = self._redecide(scope, unit_id, raw, record.shard)
                if fresh != target:
                    self._requeue(
                        scope,
                        unit_id,
                        moving,
                        fresh,
                        content_digest(raw) if fresh is not None else None,
                        index_value,
                    )
                    return "skipped"
            self._copy_kv(scope, unit_id, moving, raw)
            moving = self._advance(scope, unit_id, moving, _STEP_COPY_DERIVED)
            self._copy_derived(scope, unit_id, moving)
            moving = self._advance(scope, unit_id, moving, _STEP_SWITCH)
            self._verify_source_unchanged(scope, unit_id, moving, raw)
            switched = self._switch(scope, unit_id, moving)
        except ConflictError:
            logger.warning(
                "relocation of %s lost placement ownership; leave it for stale recovery",
                unit_id,
            )
            return "failed"
        except _SwitchUnsafe:
            # 目标副本不可用（回滚残留 / 写入丢失）：回滚，绝不切换到空目标。
            logger.warning(
                "relocation of %s cannot switch: target copy missing or diverged; "
                "rolling back",
                unit_id,
            )
            outcome = self._rollback(scope, unit_id, moving, target, index_value)
            return "abandoned" if outcome == "abandoned" else "failed"
        except _SourceChanged:
            # 并发 update 改写了源数据：不计失败次数，回滚后下一轮按新内容重新裁决。
            outcome = self._rollback(
                scope, unit_id, moving, target, index_value, count_failure=False
            )
            return "failed" if outcome == "error" else "skipped"
        except Exception as exc:  # noqa: BLE001 - 单条失败回滚后继续其它记录
            logger.warning(
                "relocation of %s failed (%s); rolling back", unit_id, type(exc).__name__
            )
            outcome = self._rollback(scope, unit_id, moving, target, index_value)
            return "abandoned" if outcome == "abandoned" else "failed"
        return "moved" if self._cleanup(scope, unit_id, switched, index_value) else "failed"

    def _advance(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        step: str,
    ) -> PlacementRecord:
        advanced = replace(record, step=step)
        if not self._cas(scope, unit_id, record, advanced):
            raise ConflictError("memory_relocation", unit_id)
        return advanced

    def _copy_kv(
        self, scope: Scope, unit_id: str, record: PlacementRecord, raw: bytes
    ) -> None:
        source = self._stores[record.shard]
        target = self._stores[record.relocate_to or ""]
        key = memory_key(unit_id)
        ttl = source.remaining_ttl(scope, key)
        if ttl is None:
            raise NotFoundError("memory", unit_id)
        self._put_copy(target, scope, key, raw, ttl)

    @staticmethod
    def _put_copy(target: KVStore, scope: Scope, key: str, raw: bytes, ttl: float) -> None:
        try:
            target.insert(scope, key, raw, ttl=ttl)
        except ConflictError:
            if target.get(scope, key) != raw:
                raise

    def _copy_derived(self, scope: Scope, unit_id: str, record: PlacementRecord) -> None:
        for port in self._ports():
            port.relocate_unit(scope, unit_id, record.shard, record.relocate_to)

    def _verify_source_unchanged(
        self, scope: Scope, unit_id: str, record: PlacementRecord, copied: bytes
    ) -> None:
        """切换前确认源数据仍是复制时的版本。

        ``update`` 先读归属、后写数据：读到 ``active`` 之后才被改成 ``moving`` 的 update
        仍会写进源分片。若它的写入晚于复制，切换会丢掉这次修改；此处发现即回滚，
        下一轮按新内容重新搬（update 自身在写后复核归属时也会返回可重试错误）。
        """
        current = self._stores[record.shard].get(scope, memory_key(unit_id))
        if current != copied:
            raise _SourceChanged(unit_id)

    def _verify_switch_target(
        self, scope: Scope, unit_id: str, record: PlacementRecord
    ) -> None:
        """切换前确认目标 KV 副本存在且（源仍在时）与源一致。

        这是 ``step=switch`` 遗留记录的安全闸：回滚失败可能删掉目标副本，只凭 step
        前滚会切换到一个空目标并删除唯一有效数据。
        """
        key = memory_key(unit_id)
        target_store = self._stores[record.relocate_to or ""]
        if not target_store.exists(scope, key):
            raise _SwitchUnsafe(unit_id)
        source_store = self._stores[record.shard]
        if source_store.exists(scope, key):
            if target_store.get(scope, key) != source_store.get(scope, key):
                raise _SwitchUnsafe(unit_id)

    def _switch(self, scope: Scope, unit_id: str, record: PlacementRecord) -> PlacementRecord:
        """复核目标副本后切换归属，并在同一次条件替换里取得源分片清理的独占执行权。"""
        self._verify_switch_target(scope, unit_id, record)
        target = record.relocate_to or ""
        switched = PlacementRecord(
            shard=target,
            status=PlacementStatus.ACTIVE,
            token=record.token,
            created_at=self._placement.now(),
            cleanup_from=record.shard,
            claim=str(uuid.uuid4()),
            claimed_at=self._placement.now(),
        )
        if not self._cas(scope, unit_id, record, switched):
            raise ConflictError("memory_relocation", unit_id)
        return switched

    # -- 清理（独占执行权） ------------------------------------------------------ #

    def _cleanup(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        index_value: bytes | None,
        *,
        take_over: bool = False,
    ) -> bool:
        """在持有 ``claim`` 的前提下清理 ``cleanup_from`` 分片中该记忆的数据。

        ``claim`` 存在期间：其它执行者不能清除 ``cleanup_from``，同 id 的新搬迁不能开始，
        delete 返回可重试错误。因此一个变慢的清理者恢复后，它删除的仍只能是本轮要删的
        那份残留，不会删到后来搬回同一分片、或删除后重新插入的数据。
        """
        source = record.cleanup_from
        if source is None:
            return True
        claimed = record  # 由 _switch 交来的记录已带本执行者的 claim
        if record.claim is None or take_over:
            claimed = replace(
                record, claim=str(uuid.uuid4()), claimed_at=self._placement.now()
            )
            if not self._cas(scope, unit_id, record, claimed):
                return False
        try:
            for port in self._ports():
                port.cleanup_unit(scope, unit_id, source)
            self._stores[source].delete(scope, memory_key(unit_id))
        except Exception:  # noqa: BLE001 - 清理失败释放执行权，下一轮在线重试
            logger.warning(
                "cleanup of %s in shard %r failed; keep cleanup_from", unit_id, source
            )
            self._update_claimed(
                scope, unit_id, claimed.claim, lambda cur: replace(
                    cur, claim=None, claimed_at=None
                )
            )
            return False
        cleared = self._update_claimed(
            scope,
            unit_id,
            claimed.claim,
            lambda cur: replace(cur, cleanup_from=None, claim=None, claimed_at=None),
        )
        if cleared is None:
            logger.warning("cleanup of %s finished but claim was lost", unit_id)
            return False
        if not _has_work(cleared):
            self._drop_index(scope, unit_id, index_value)
        return True

    def _update_claimed(
        self,
        scope: Scope,
        unit_id: str,
        claim: str | None,
        change: Callable[[PlacementRecord], PlacementRecord],
    ) -> PlacementRecord | None:
        """只在执行权仍是自己时修改记录；并发标记只改了别的字段时重读后重试。"""
        for _ in range(5):
            current = self._read(scope, unit_id)
            if current is None or claim is None or current.claim != claim:
                return None
            updated = change(current)
            if self._cas(scope, unit_id, current, updated):
                return updated
        return None

    @staticmethod
    def _claim_stale(record: PlacementRecord, now: float, stale_after: float) -> bool:
        claimed_at = record.claimed_at if record.claimed_at is not None else record.created_at
        return now - claimed_at > stale_after

    # -- 回滚 / 重新排队 -------------------------------------------------------- #

    def _rollback(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        target: str,
        index_value: bytes | None,
        *,
        count_failure: bool = True,
    ) -> str:
        """删除目标侧副本并恢复为 ``active(S, relocate_to=T)``。

        返回 ``rolled_back``（保留标记，下一轮重试）、``abandoned``（连续失败达到上限，
        清除标记）或 ``error``（回滚本身失败，保持 ``moving`` 交维护窗口）。
        """
        key = memory_key(unit_id)
        target_store = self._stores[target]
        if record.step != _STEP_ROLLBACK:
            # **先持久化「回滚中」再动数据**：回滚会先删目标派生记录、再删目标 KV；
            # 若在两步之间崩溃而记录仍停在 step=switch，维护恢复会复核到目标 KV 仍在
            # 且与源一致，从而误判为「可以前滚」，随后清理源分片，派生索引就永久丢了。
            rolling_back = replace(record, step=_STEP_ROLLBACK)
            if not self._cas(scope, unit_id, record, rolling_back):
                logger.warning("rollback of %s lost placement ownership", unit_id)
                return "error"
            record = rolling_back
        try:
            # 先清理派生记录、最后删目标 KV：若派生清理失败，目标 KV 仍在，维护
            # 恢复至少不会把归属切到一个空分片（_switch 也会再复核一次）。
            for port in self._ports():
                port.cleanup_unit(scope, unit_id, target)
            if target_store.exists(scope, key):
                target_store.delete(scope, key)
        except Exception:  # noqa: BLE001 - 回滚失败保持 moving，交维护窗口
            logger.warning("rollback cleanup of %s failed", unit_id)
            return "error"
        failures = (record.failures or 0) + (1 if count_failure else 0)
        give_up = count_failure and failures >= self._max_failures
        restored = PlacementRecord(
            shard=record.shard,
            status=PlacementStatus.ACTIVE,
            token=str(uuid.uuid4()),
            created_at=self._placement.now(),
            relocate_to=None if give_up else target,
            failures=None if give_up or failures == 0 else failures,
            mark_digest=None if give_up else record.mark_digest,
        )
        if not self._cas(scope, unit_id, record, restored):
            logger.warning("rollback of %s lost placement ownership", unit_id)
            return "error"
        if give_up:
            logger.error(
                "abandon relocation of %s to %r after %d failures; memory stays in %r",
                unit_id,
                target,
                failures,
                record.shard,
            )
            self._drop_index(scope, unit_id, index_value)
            return "abandoned"
        return "rolled_back"

    def _requeue(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        target: str | None,
        digest: str | None,
        index_value: bytes | None,
    ) -> None:
        """在尚未复制任何数据时回到 ``active(S)``，并以新的裁决结果替换标记。"""
        restored = PlacementRecord(
            shard=record.shard,
            status=PlacementStatus.ACTIVE,
            token=str(uuid.uuid4()) if record.status is PlacementStatus.MOVING else record.token,
            created_at=self._placement.now(),
            relocate_to=target,
            mark_digest=digest if target is not None else None,
            failures=record.failures if target is not None else None,
        )
        if self._cas(scope, unit_id, record, restored) and target is None:
            self._drop_index(scope, unit_id, index_value)

    def _redecide(self, scope: Scope, unit_id: str, raw: bytes, shard: str) -> str | None:
        unit = load_memory_unit(raw, scope=scope, key=memory_key(unit_id))
        if unit is None:
            return None
        try:
            target = self._resolver.resolve_on_update(unit, unit, shard)
        except ValidationError:
            return None
        if target is not None and (target not in self._stores or self._is_retiring(target)):
            return None
        return target

    # -- 遗留 moving 恢复 ------------------------------------------------------ #

    def _recover_one(
        self,
        scope: Scope,
        unit_id: str,
        record: PlacementRecord,
        index_value: bytes | None,
    ) -> bool:
        step = record.step or _STEP_COPY_KV
        if step == _STEP_ROLLBACK or step in _STEPS_BEFORE_SWITCH:
            # 「回滚中」不能前滚：目标副本可能已被删除 / 派生记录已清理，前滚会
            # 清理源分片并永久丢掉唯一的派生索引。继续（幂等地）完成回滚。
            outcome = self._rollback(
                scope, unit_id, record, record.relocate_to or "", index_value
            )
            return outcome != "error"
        try:
            switched = self._switch(scope, unit_id, record)
        except _SwitchUnsafe:
            logger.warning(
                "stale relocation of %s cannot switch: target copy missing or "
                "diverged; rolling back",
                unit_id,
            )
            outcome = self._rollback(
                scope, unit_id, record, record.relocate_to or "", index_value
            )
            return outcome != "error"
        except ConflictError:
            return False
        except Exception:  # noqa: BLE001
            return False
        return self._cleanup(scope, unit_id, switched, index_value)

    # -- 待搬迁索引 ------------------------------------------------------------ #

    def _touch_index(self, scope: Scope, unit_id: str) -> bool:
        """写入一个新的索引版本（时间 + 随机串），让旧版本的条件删除必然失败。

        CAS 失败不能假定「另一个写者已经刷新了索引」：旧清理任务可能刚好按旧值删掉了
        它，此时标记会指向一个不存在的索引、永不被扫描。这里重读后重试创建 / 替换；
        连续失败则返回 ``False``，调用方宁可不写标记，也不留下没有索引的任务。
        """
        key = relocation_index_key(unit_id)
        value = f"{time.time():.6f}:{uuid.uuid4().hex}".encode()
        try:
            for _ in range(4):
                try:
                    self._placement.create_if_absent(scope, key, value)
                    return True
                except ConflictError:
                    pass
                current = self._placement.read_many(scope, [key]).get(key)
                if current is None:
                    continue  # 索引刚被清理任务删除：重新创建
                try:
                    self._placement.replace_if_value(scope, key, current, value)
                    return True
                except ConflictError:
                    continue  # 版本已变：按新值重试，不能假定别人已刷新
        except Exception:  # noqa: BLE001 - 索引写不进去时不留下无索引的标记
            logger.exception("relocation index update failed for %s", unit_id)
            return False
        logger.error(
            "could not refresh relocation index for %s after retries", unit_id
        )
        return False

    def _drop_index(self, scope: Scope, unit_id: str, index_value: bytes | None) -> None:
        """只删除本轮读到的那个索引版本。

        ``index_value`` 必须在读取归属记录**之前**读到：之后任何新标记都会先写入新的
        索引版本，条件删除因此失败，新任务的索引得以保留。不读取最新值再删。
        """
        if index_value is None:
            return
        self._placement.delete_if_value(scope, relocation_index_key(unit_id), index_value)

    def _drop_stale_index(
        self, scope: Scope, unit_id: str, value: bytes, now: float, stale_after: float
    ) -> None:
        """索引指向的记录已无搬迁工作：超过 ``stale_after`` 才按读到的值条件删除。

        不立即删除，是为了不和「先写索引、后写标记」之间的窗口竞争。
        """
        try:
            written_at = float(value.decode().split(":", 1)[0])
        except ValueError:  # UnicodeDecodeError is a ValueError subclass
            written_at = 0.0
        if now - written_at <= stale_after:
            return
        self._placement.delete_if_value(scope, relocation_index_key(unit_id), value)

    def _pending(self, scope: Scope):
        """先扫索引、后批量读归属：索引版本一定早于据以决策的归属记录。"""
        entries = self._placement.scan_entries(scope, RELOCATION_INDEX_PREFIX)
        if not entries:
            return
        unit_ids = [key[len(RELOCATION_INDEX_PREFIX):] for key, _ in entries]
        raw = self._placement.read_many(scope, [placement_key(uid) for uid in unit_ids])
        for (_, index_value), unit_id in zip(entries, unit_ids, strict=True):
            value = raw.get(placement_key(unit_id))
            record = None
            if value is not None:
                try:
                    record = PlacementRecord.from_bytes(value)
                except ValidationError:
                    continue  # 坏记录交给对账
            yield unit_id, index_value, record

    # -- 内部 ------------------------------------------------------------------ #

    def _read_index(self, scope: Scope, unit_id: str) -> bytes | None:
        key = relocation_index_key(unit_id)
        return self._placement.read_many(scope, [key]).get(key)

    def _read(self, scope: Scope, unit_id: str) -> PlacementRecord | None:
        value = self._placement.read_many(scope, [placement_key(unit_id)]).get(
            placement_key(unit_id)
        )
        return None if value is None else PlacementRecord.from_bytes(value)

    def _cas(
        self,
        scope: Scope,
        unit_id: str,
        expected: PlacementRecord,
        value: PlacementRecord,
    ) -> bool:
        try:
            self._placement.replace_if_value(
                scope,
                placement_key(unit_id),
                expected.to_bytes(),
                value.to_bytes(),
            )
        except ConflictError:
            return False
        return True

    def _scopes(self) -> list[Scope]:
        """归属存储同时是 KVStore（S06 约束），待搬迁索引与归属同 Scope 落库。"""
        placement = cast(KVStore, self._placement)
        seen: dict[tuple[str, str, str, str, str], Scope] = {}
        for scope in placement.scopes():
            seen[(scope.org, scope.space, scope.user, scope.agent, scope.session)] = scope
        return list(seen.values())


def content_digest(raw: bytes) -> str:
    """打标记时与执行搬迁时比对用的正文摘要（同一份明文字节得到同一摘要）。"""
    return hashlib.sha256(raw).hexdigest()


def _has_work(record: PlacementRecord) -> bool:
    if record.status is PlacementStatus.MOVING:
        return True
    return record.status is PlacementStatus.ACTIVE and (
        record.relocate_to is not None or record.cleanup_from is not None
    )
