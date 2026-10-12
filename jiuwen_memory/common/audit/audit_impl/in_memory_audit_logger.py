# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""最小实现：:class:`~jiuwen_memory.common.audit.base.AuditLogger` 的纯内存审计后端。

把所有审计事件留在内存里，供控制层治理 audit 按条件过滤查询。同时实现
:class:`~jiuwen_memory.common.security.audit_integrity.chain_store.ChainedAuditStore`：
完整性模式下经 ``append`` 写入带 proof 的链式记录，``query`` 自动从链读取（脱去 proof）。

**两种模式不混用**（计划 §4.1）：完整性模式经 ``append``（链），普通模式经 ``record``
（``events``）。``query`` 据链是否非空选择数据源。只保证**共享同一实例**时的进程内
原子性（计划 §1.1）：跨进程持久化由 SQLite 后端承担。
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from datetime import UTC, datetime

from jiuwen_memory.common.audit.base import AuditLogger, AuditProducer
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    CANONICAL_FORMAT_VERSION,
)
from jiuwen_memory.common.security.audit_integrity.base import AuditSchemaError, ChainConflictError
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
)
from jiuwen_memory.common.type_def import AuditEvent

# 进程内后端的能力声明（计划 §4.3）。persistent=False：重启即失，故 is_test_only。
_IN_MEM_CAP = ChainStoreCapability(
    persistent=False,
    atomic_append=True,
    stable_head_snapshot=True,
    key_epoch=True,
    external_anchor=False,
    streaming_scan=True,
)


class InMemoryAuditLogger(AuditLogger, ChainedAuditStore):
    """内存审计后端：普通记录/查询 + 显式链式完整性 capability（计划 §1.1）。"""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        # 链式完整性状态（与 self.events 互斥使用）。
        self._chain_records: list[ChainedRecord] = []
        self._chain_head = ChainHead(
            sequence=0,
            digest=GENESIS_DIGEST,
            key_id="",
            key_epoch=0,
            format_version=CANONICAL_FORMAT_VERSION,
        )
        self._chain_lock = threading.Lock()

    # -- AuditLogger 契约 -------------------------------------------------- #

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        out: list[AuditEvent] = []
        for event in self._query_source():
            if not _matches(event, filters):
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out

    def _query_source(self) -> list[AuditEvent]:
        """完整性模式下从链读取（脱 proof），否则从普通 events 读取（计划 §4.2）。"""
        with self._chain_lock:
            if self._chain_records:
                return [r.event for r in self._chain_records]
        return self.events

    # -- ChainedAuditStore 契约 ------------------------------------------- #

    def capabilities(self) -> ChainStoreCapability:
        return _IN_MEM_CAP

    def read_head(self) -> ChainHead:
        with self._chain_lock:
            return self._chain_head

    def append(self, record: ChainedRecord, expected_head: ChainHead) -> ChainHead:
        # 实例级锁 + CAS：共享同一实例的两个 writer 不分叉（计划 §1.3 经验 1、§4.3）。
        with self._chain_lock:
            if (
                expected_head.sequence != self._chain_head.sequence
                or expected_head.digest != self._chain_head.digest
            ):
                raise ChainConflictError("in-memory chain head moved")
            self._chain_records.append(record)
            self._chain_head = ChainHead(
                sequence=record.proof.sequence,
                digest=record.proof.digest,
                key_id=record.proof.key_id,
                key_epoch=record.proof.key_epoch,
                format_version=record.proof.format_version,
            )
            return self._chain_head

    def read_stable_snapshot(self, after_sequence: int = 0) -> ChainSnapshot:
        # 实例级锁保证 head、末事件与 checkpoint 一致读取（计划 §1.3 经验 3），并在
        # 同一快照内核对 head 与末记录 / genesis 固定值（R4：与 SQLite 后端同一条
        # 契约，不因后端不同而少检字段）。
        with self._chain_lock:
            self._check_head_locked()
            last = self._chain_records[-1] if self._chain_records else None
            checkpoint = None
            if after_sequence > 0:
                for record in self._chain_records:
                    if record.proof.sequence == after_sequence:
                        checkpoint = record
                        break
            return ChainSnapshot(
                head=self._chain_head,
                last_record=last,
                after_sequence=after_sequence,
                checkpoint=checkpoint,
            )

    def scan(
        self,
        after_sequence: int,
        limit: int,
        *,
        through_sequence: int,
    ) -> list[ChainedRecord]:
        # 有界 keyset 读取（PR3-10，计划 §12.5）：sequence 严格升序（append CAS 保证
        # =head+1），二分定位窗口端点，按 ``limit`` 切片；不建整个剩余窗口的临时列表，
        # 也不逐页从链首扫全链。
        if limit < 1:
            return []
        with self._chain_lock:
            start = bisect_right(
                self._chain_records, after_sequence, key=lambda r: r.proof.sequence
            )
            end = bisect_right(
                self._chain_records, through_sequence, key=lambda r: r.proof.sequence
            )
            stop = min(end, start + limit)
            return self._chain_records[start:stop]

    def health(self) -> None:
        # 进程内链无 schema；校验 head 与末记录一致性 / genesis 固定值
        # （计划 §1.3 经验 3；R4：与 SQLite 后端同一契约）。
        with self._chain_lock:
            self._check_head_locked()

    def _check_head_locked(self) -> None:
        """核对本实例 head 与末记录 / genesis 固定值（调用方须已持 ``_chain_lock``）。"""
        head = self._chain_head
        last = self._chain_records[-1] if self._chain_records else None
        if head.sequence == 0:
            if last is not None:
                raise AuditSchemaError("in-memory chain head at genesis but records exist")
            if head.digest != GENESIS_DIGEST:
                raise AuditSchemaError("in-memory genesis head digest != GENESIS_DIGEST")
            if head.key_id != "" or head.key_epoch != 0:
                raise AuditSchemaError("in-memory genesis head must carry an empty key ref")
            if head.format_version != CANONICAL_FORMAT_VERSION:
                raise AuditSchemaError(
                    f"in-memory chain head format_version {head.format_version} unsupported"
                )
            return
        if last is None:
            raise AuditSchemaError("in-memory chain head beyond genesis but no records")
        proof = last.proof
        if head.sequence != proof.sequence:
            raise AuditSchemaError("in-memory chain head sequence != last record sequence")
        if head.digest != proof.digest:
            raise AuditSchemaError("in-memory chain head digest != last record proof digest")
        if head.key_id != proof.key_id or head.key_epoch != proof.key_epoch:
            raise AuditSchemaError("in-memory chain head key ref != last record proof key ref")
        if head.format_version != CANONICAL_FORMAT_VERSION:
            raise AuditSchemaError(
                f"in-memory chain head format_version {head.format_version} unsupported"
            )


# -- 注册到 AuditProducer（实现自注册，新增无需改 producer/make_plugins） ------ #


@AuditProducer.register("in_memory")
def _build(config):
    return InMemoryAuditLogger()


def _matches(event: AuditEvent, filters: dict[str, str]) -> bool:
    for field in ("action", "layer", "decision", "target_id"):
        if filters.get(field) and getattr(event, field) != filters[field]:
            return False

    actor_filters = {
        "actor_org": event.actor.org,
        "actor_space": event.actor.space,
        "actor_user": event.actor.user,
        "actor_agent": event.actor.agent,
        "actor_session": event.actor.session,
    }
    for field, value in actor_filters.items():
        if filters.get(field) and value != filters[field]:
            return False

    target_filters = {
        "target_org": event.target.org,
        "target_space": event.target.space,
        "target_user": event.target.user,
        "target_agent": event.target.agent,
        "target_session": event.target.session,
    }
    for field, value in target_filters.items():
        if filters.get(field) and value != filters[field]:
            return False

    occurred_at = _as_utc(event.occurred_at)
    after = _parse_datetime(filters.get("occurred_after"))
    if after is not None and (occurred_at is None or occurred_at < after):
        return False
    before = _parse_datetime(filters.get("occurred_before"))
    if before is not None and (occurred_at is None or occurred_at > before):
        return False
    return True


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return _as_utc(datetime.fromisoformat(raw))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
