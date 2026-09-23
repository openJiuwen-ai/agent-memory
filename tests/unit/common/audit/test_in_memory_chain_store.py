# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段 B：InMemory ChainStore capability 的原子性与一致性（计划 §12.2 原子写与恢复）。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    CANONICAL_FORMAT_VERSION,
    ChainedHmacAuditIntegrityProvider,
)
from jiuwen_memory.common.security.audit_integrity.base import AuditSchemaError, ChainConflictError
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    ChainedRecord,
    ChainHead,
)
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import AuditEvent
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit

_KEY_HEX = "33" * 32


def _kp() -> LocalKeyProvider:
    return LocalKeyProvider(key_hex=_KEY_HEX, create_key_file=False)


def _event(event_id: str = "e") -> AuditEvent:
    return AuditEvent(
        id=event_id,
        actor=Scope(org="acme", user="u1"),
        target=Scope(org="acme", space="s1"),
        action="write",
        target_id="t",
        layer="api",
        decision="allow",
        occurred_at=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC),
        detail={"role": "user"},
    )


def _provider(store=None, **kw) -> ChainedHmacAuditIntegrityProvider:
    return ChainedHmacAuditIntegrityProvider(store or InMemoryAuditLogger(), _kp(), **kw)


def test_capabilities_declared() -> None:
    store = InMemoryAuditLogger()
    cap = store.capabilities()
    assert cap.atomic_append
    assert cap.stable_head_snapshot
    assert cap.key_epoch
    assert cap.streaming_scan
    assert cap.persistent is False  # 进程内，is_test_only


def test_genesis_head_before_any_record() -> None:
    store = InMemoryAuditLogger()
    head = store.read_head()
    assert head.sequence == 0
    assert head.digest == GENESIS_DIGEST


def test_append_advances_head_and_cas_rejects_stale() -> None:
    store = InMemoryAuditLogger()
    provider = _provider(store)
    rec1 = provider.record_chained(_event("a"))
    head_after_first = store.read_head()
    # 用过期 expected_head 追加应冲突
    stale = ChainHead(
        sequence=0,
        digest=GENESIS_DIGEST,
        key_id="",
        key_epoch=0,
        format_version=CANONICAL_FORMAT_VERSION,
    )
    rec2 = ChainedRecord(event=_event("b"), proof=rec1.proof)  # proof 仅占位
    with pytest.raises(ChainConflictError):
        store.append(rec2, expected_head=stale)
    # head 未被冲突推进
    assert store.read_head() == head_after_first


def test_concurrent_writers_no_fork() -> None:
    """两个线程共享同一内存 store 并发追加不分叉（计划 §1.3 经验 1、§12.2）。"""
    store = InMemoryAuditLogger()
    # 高压竞争下提高重试上限以吸收调度抖动（生产默认 10 适用于低竞争审计路径）。
    provider = _provider(store, max_cas_retries=200)
    n = 200
    errors: list[Exception] = []

    def writer(start: int) -> None:
        try:
            for i in range(start, start + n):
                provider.record_chained(_event(f"t-{i}"))
        except Exception as exc:  # noqa: BLE001 - 收集后断言
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(0,))
    t2 = threading.Thread(target=writer, args=(n,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    head = store.read_head()
    assert head.sequence == 2 * n  # 无丢失、无分叉
    result = provider.verify()
    assert result.status.value == "clean"
    assert result.checked_count == 2 * n


def test_stable_snapshot_head_matches_last() -> None:
    store = InMemoryAuditLogger()
    provider = _provider(store)
    provider.record_chained(_event("a"))
    provider.record_chained(_event("b"))
    snap = store.read_stable_snapshot()
    assert snap.head.sequence == 2
    assert snap.last_record is not None
    assert snap.last_record.proof.sequence == 2
    assert snap.head.digest == snap.last_record.proof.digest


def test_stable_snapshot_returns_requested_checkpoint() -> None:
    """after_sequence>0 时快照携带该序号的 checkpoint（增量验证依据，F05 §6.1）。"""
    store = InMemoryAuditLogger()
    provider = _provider(store)
    for i in range(3):
        provider.record_chained(_event(f"e{i}"))
    snap = store.read_stable_snapshot(after_sequence=2)
    assert snap.after_sequence == 2
    assert snap.checkpoint is not None
    assert snap.checkpoint.proof.sequence == 2
    # 不存在的序号：checkpoint 为 None
    snap_missing = store.read_stable_snapshot(after_sequence=99)
    assert snap_missing.checkpoint is None


def test_scan_keyset_pagination() -> None:
    store = InMemoryAuditLogger()
    provider = _provider(store)
    for i in range(10):
        provider.record_chained(_event(f"e{i}"))
    through = store.read_head().sequence
    page1 = store.scan(0, 4, through_sequence=through)
    page2 = store.scan(page1[-1].proof.sequence, 4, through_sequence=through)
    page3 = store.scan(page2[-1].proof.sequence, 4, through_sequence=through)
    seqs = [r.proof.sequence for r in page1 + page2 + page3]
    assert seqs == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_query_reads_from_chain_in_integrity_mode() -> None:
    """完整性模式下 query() 从链读取（脱 proof），计划 §4.2。"""
    store = InMemoryAuditLogger()
    provider = _provider(store)
    provider.record_chained(_event("a"))
    provider.record_chained(_event("b"))
    events = store.query({}, limit=100)
    assert len(events) == 2
    assert [e.id for e in events] == ["a", "b"]


def test_query_reads_from_events_in_plain_mode() -> None:
    """普通模式 query() 仍读 self.events（未启用完整性）。"""
    store = InMemoryAuditLogger()
    store.record(_event("a"))
    store.record(_event("b"))
    assert [e.id for e in store.query({}, limit=100)] == ["a", "b"]


def test_health_detects_head_last_mismatch() -> None:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = InMemoryAuditLogger()
    provider = _provider(store)
    provider.record_chained(_event("a"))
    # 直接篡改 head 制造不一致
    store._chain_head = ChainHead(  # noqa: SLF001 - 测试需直接篡改内存 head
        sequence=99,
        digest="x" * 64,
        key_id=store._chain_head.key_id,
        key_epoch=store._chain_head.key_epoch,
        format_version=CANONICAL_FORMAT_VERSION,
    )
    with pytest.raises(AuditSchemaError):
        store.health()


def test_scan_window_bounds_and_bounded_reads() -> None:
    """scan 窗口边界与限量读取（PR3-10，计划 §12.5）：

    - ``after_sequence`` / ``through_sequence`` 恰在链内 sequence 上时开闭边界正确；
    - 大窗口 + 小 ``limit`` 只返回 ``limit`` 条（有序定位 + 限量切片，不建整个
      剩余窗口的临时列表）；
    - ``limit < 1`` 返回空。
    """
    store = InMemoryAuditLogger()
    provider = _provider(store)
    for i in range(10):
        provider.record_chained(_event(f"e{i}"))
    through = store.read_head().sequence

    # 开闭边界：(after, through]，两端恰好压在链内 sequence 上。
    seqs = [r.proof.sequence for r in store.scan(3, 10, through_sequence=7)]
    assert seqs == [4, 5, 6, 7]

    # 大窗口 + 小 limit：只读取/返回 limit 条。
    assert len(store.scan(0, 3, through_sequence=through)) == 3
    assert len(store.scan(5, 2, through_sequence=through)) == 2

    # limit < 1：空页，不触发读取。
    assert store.scan(0, 0, through_sequence=through) == []


# ====================================================================== #
# 复验故障样本镜像（security-plans/problems/2026-09-22-pr3-reacceptance.md
# R4：内存后端与 SQLite 同一条 head/genesis 核对契约）
# ====================================================================== #


def _chain_one(provider=None) -> InMemoryAuditLogger:
    store = InMemoryAuditLogger()
    _provider(store).record_chained(_event("a"))
    return store


def test_memory_head_sequence_tamper_rejected() -> None:
    """
    有记录的链把 head.sequence 改回 0（R4）：内存后端同样拒绝——head 在 genesis
    而记录存在即不一致，不得返回 checked_count=0 的 clean。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _chain_one()
    store._chain_head = ChainHead(  # noqa: SLF001 - 攻击注入需绕过链直接篡改 head
        sequence=0,
        digest=store._chain_head.digest,  # noqa: SLF001
        key_id=store._chain_head.key_id,  # noqa: SLF001
        key_epoch=store._chain_head.key_epoch,  # noqa: SLF001
        format_version=store._chain_head.format_version,  # noqa: SLF001
    )
    with pytest.raises(AuditSchemaError):
        _provider(store).verify()
    with pytest.raises(AuditSchemaError):
        store.health()


def test_memory_head_key_ref_tamper_rejected() -> None:
    """head 的 key ref 与末 proof 不一致（R4）：内存后端逐字段核对 key_id/key_epoch。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _chain_one()
    store._chain_head = ChainHead(  # noqa: SLF001
        sequence=1,
        digest=store._chain_head.digest,  # noqa: SLF001
        key_id="forged",
        key_epoch=store._chain_head.key_epoch,  # noqa: SLF001
        format_version=CANONICAL_FORMAT_VERSION,
    )
    with pytest.raises(AuditSchemaError):
        _provider(store).verify()


def test_memory_genesis_digest_tamper_rejected() -> None:
    """空链 genesis digest 被篡改（R4）：内存后端核对 genesis 固定值。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = InMemoryAuditLogger()
    store.health()
    store._chain_head = ChainHead(  # noqa: SLF001
        sequence=0,
        digest="forged",
        key_id="",
        key_epoch=0,
        format_version=CANONICAL_FORMAT_VERSION,
    )
    with pytest.raises(AuditSchemaError):
        _provider(store).verify()
    with pytest.raises(AuditSchemaError):
        store.health()
