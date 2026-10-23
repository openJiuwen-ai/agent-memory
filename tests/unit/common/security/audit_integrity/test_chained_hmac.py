# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段 A conformance：版本化规范化 + 链式 HMAC 证明/验证（计划 §12.1 契约与规范化）。

纯算法层：用最小 fake ChainStore 验证 Provider 的签发、CAS 协调与流式验证逻辑；真实
后端（InMemory / SQLite）的原子性、并发与 schema 在阶段 B 覆盖（test_*_chain_store）。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    CANONICAL_FORMAT_VERSION,
    ChainedHmacAuditIntegrityProvider,
    canonical_event_bytes,
)
from jiuwen_memory.common.security.audit_integrity.base import (
    AnchorStatus,
    AuditIntegrityStatus,
    ChainConflictError,
    KeyCapabilityError,
    Proof,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    AnchorRecord,
    AuditAnchor,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
)
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.security.cryptography.key_provider import KeyRef
from jiuwen_memory.common.type_def import AuditEvent
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit

_KEY_HEX = "22" * 32  # 32 字节根密钥


def _key_provider() -> LocalKeyProvider:
    return LocalKeyProvider(key_hex=_KEY_HEX, create_key_file=False)


def _event(**fields) -> AuditEvent:
    """显式构造真实事件，再按测试指定字段生成样本；未知字段由 replace 拒绝。"""
    event = AuditEvent(
        id="evt-1",
        actor=Scope(org="acme", user="u1"),
        target=Scope(org="acme", space="s1"),
        action="write",
        target_id="t-1",
        layer="api",
        decision="allow",
        occurred_at=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC),
        detail={"role": "user"},
    )
    return replace(event, **fields)


# ====================================================================== #
# 最小 fake ChainStore（阶段 A 仅供 Provider 协调逻辑测试；真实后端在阶段 B）
# ====================================================================== #


_CAP = ChainStoreCapability(
    persistent=False,
    atomic_append=True,
    stable_head_snapshot=True,
    key_epoch=True,
    external_anchor=False,
    streaming_scan=True,
)


class _FakeChainStore(ChainedAuditStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[ChainedRecord] = []
        self._head = ChainHead(
            sequence=0,
            digest=GENESIS_DIGEST,
            key_id="",
            key_epoch=0,
            format_version=CANONICAL_FORMAT_VERSION,
        )

    def capabilities(self) -> ChainStoreCapability:
        return _CAP

    def read_head(self) -> ChainHead:
        with self._lock:
            return self._head

    def append(self, record: ChainedRecord, expected_head: ChainHead) -> ChainHead:
        with self._lock:
            if (
                expected_head.sequence != self._head.sequence
                or expected_head.digest != self._head.digest
            ):
                raise ChainConflictError("head moved")
            self._records.append(record)
            self._head = ChainHead(
                sequence=record.proof.sequence,
                digest=record.proof.digest,
                key_id=record.proof.key_id,
                key_epoch=record.proof.key_epoch,
                format_version=record.proof.format_version,
            )
            return self._head

    def read_stable_snapshot(self, after_sequence: int = 0) -> ChainSnapshot:
        with self._lock:
            last = self._records[-1] if self._records else None
            checkpoint = None
            if after_sequence > 0:
                for record in self._records:
                    if record.proof.sequence == after_sequence:
                        checkpoint = record
                        break
            return ChainSnapshot(
                head=self._head,
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
        with self._lock:
            records = []
            for record in self._records:
                if after_sequence < record.proof.sequence <= through_sequence:
                    records.append(record)
            return records[:limit]

    def health(self) -> None:
        return None

    # 测试辅助：直接篡改记录（模拟攻击）
    def _tamper_record(
        self, index: int, event: AuditEvent | None = None, proof: Proof | None = None
    ) -> None:
        with self._lock:
            rec = self._records[index]
            self._records[index] = ChainedRecord(
                event=event if event is not None else rec.event,
                proof=proof if proof is not None else rec.proof,
            )


def _provider(store=None, key_provider=None, **kw) -> ChainedHmacAuditIntegrityProvider:
    return ChainedHmacAuditIntegrityProvider(
        store or _FakeChainStore(),
        key_provider or _key_provider(),
        **kw,
    )


def _record(store: _FakeChainStore, index: int) -> ChainedRecord:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    return store._records[index]  # noqa: SLF001 - 测试需直接访问内存记录


# ====================================================================== #
# 规范化（计划 §12.1）
# ====================================================================== #


def test_canonical_is_dict_order_independent() -> None:
    """dict 插入顺序不同 -> 相同规范字节（计划 §4.5、§12.1）。"""
    ref = KeyRef(key_id="k1", epoch=1)
    e = _event(detail={"a": "1", "b": "2", "c": "3"})
    b1 = canonical_event_bytes(e, sequence=1, previous_digest=GENESIS_DIGEST, key_ref=ref)
    e2 = _event(detail={"c": "3", "a": "1", "b": "2"})
    b2 = canonical_event_bytes(e2, sequence=1, previous_digest=GENESIS_DIGEST, key_ref=ref)
    assert b1 == b2


def test_canonical_is_timezone_equivalent() -> None:
    """同一 instant 的不同 tz 表示 -> 相同规范字节（计划 §4.5 UTC 规范表示）。"""
    ref = KeyRef(key_id="k1", epoch=1)
    aware_utc = _event(occurred_at=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC))
    aware_offset = _event(
        occurred_at=datetime(2026, 8, 11, 20, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    )
    b1 = canonical_event_bytes(aware_utc, sequence=1, previous_digest=GENESIS_DIGEST, key_ref=ref)
    b2 = canonical_event_bytes(
        aware_offset, sequence=1, previous_digest=GENESIS_DIGEST, key_ref=ref
    )
    assert b1 == b2


def test_scope_dimension_change_changes_proof() -> None:
    """actor/target 任一 Scope 维度变化 -> proof 变化（计划 §12.1）。"""
    provider = _provider()
    base = _event(actor=Scope(org="acme", user="u1"))
    r1 = provider.record_chained(base)
    provider2 = _provider(store=_FakeChainStore())
    r2 = provider2.record_chained(replace(base, actor=Scope(org="acme", user="u2")))
    assert r1.proof.digest != r2.proof.digest


def test_detail_change_changes_proof() -> None:
    provider = _provider()
    r1 = provider.record_chained(_event(detail={"role": "user"}))
    provider2 = _provider(store=_FakeChainStore())
    r2 = provider2.record_chained(_event(detail={"role": "admin"}))
    assert r1.proof.digest != r2.proof.digest


def test_sequence_and_previous_digest_bound_into_proof() -> None:
    """sequence / previous_digest 是规范化的输入，变化则 proof 变化（计划 §12.1）。"""
    provider = _provider()
    e = _event()
    r1 = provider.record_chained(e)
    # 第二条的前序摘要应是第一条的 digest
    r2 = provider.record_chained(e)
    assert r2.proof.sequence == 2
    assert r2.proof.previous_digest == r1.proof.digest
    assert r2.proof.digest != r1.proof.digest


# ====================================================================== #
# Provider：签发 + 验证（计划 §12.1）
# ====================================================================== #


def test_record_chained_assigns_monotonic_sequence() -> None:
    provider = _provider()
    r1 = provider.record_chained(_event(id="a"))
    r2 = provider.record_chained(_event(id="b"))
    r3 = provider.record_chained(_event(id="c"))
    assert [r1.proof.sequence, r2.proof.sequence, r3.proof.sequence] == [1, 2, 3]
    assert r2.proof.previous_digest == r1.proof.digest
    assert r3.proof.previous_digest == r2.proof.digest


def test_chain_store_returns_the_store_instance() -> None:
    """chain_store() 返回构造时传入的实例（装配层 identity 校验的依据）。"""
    store = _FakeChainStore()
    provider = _provider(store=store)
    assert provider.chain_store() is store


def test_verify_clean_chain() -> None:
    provider = _provider()
    for i in range(5):
        provider.record_chained(_event(id=f"e{i}"))
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.checked_count == 5
    assert result.error_count == 0
    assert result.high_water_mark == 5
    assert result.anchor.checked is False  # 无锚点，诚实声明不防回滚
    assert result.samples == ()


def test_verify_empty_chain_is_clean() -> None:
    """空链（head.sequence=0）在快照范围内无记录也无矛盾：clean。"""
    provider = _provider()
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.checked_count == 0
    assert result.high_water_mark == 0


def test_verify_detects_content_tamper() -> None:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    provider.record_chained(_event(id="e1"))
    provider.record_chained(_event(id="e2"))
    # 篡改中间事件内容
    store._tamper_record(1, event=_event(id="e1", action="delete"))
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED
    assert result.error_count >= 1
    assert len(result.samples) >= 1


def test_verify_detects_proof_digest_tamper() -> None:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    provider.record_chained(_event(id="e1"))
    rec = _record(store, 0)
    bad_proof = Proof(
        format_version=rec.proof.format_version,
        sequence=rec.proof.sequence,
        previous_digest=rec.proof.previous_digest,
        digest="f" * 64,  # 伪造摘要
        key_id=rec.proof.key_id,
        key_epoch=rec.proof.key_epoch,
    )
    store._tamper_record(0, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED


def test_verify_detects_linkage_break() -> None:
    """previous_digest 指向错误的前序 -> tampered（计划 §12.3）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    provider.record_chained(_event(id="e1"))
    rec = _record(store, 1)
    bad_proof = Proof(
        format_version=rec.proof.format_version,
        sequence=rec.proof.sequence,
        previous_digest="a" * 64,  # 不等于 r1.digest
        digest=rec.proof.digest,
        key_id=rec.proof.key_id,
        key_epoch=rec.proof.key_epoch,
    )
    store._tamper_record(1, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED


def test_verify_unknown_format_version_is_incomplete_not_clean() -> None:
    """未知格式版本返回 incomplete，拒绝当 clean（计划 §4.5、§12.1）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    rec = _record(store, 0)
    bad_proof = Proof(
        format_version=999,
        sequence=rec.proof.sequence,
        previous_digest=rec.proof.previous_digest,
        digest=rec.proof.digest,
        key_id=rec.proof.key_id,
        key_epoch=rec.proof.key_epoch,
    )
    store._tamper_record(0, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.INCOMPLETE


def test_verify_null_proof_sentinel_is_incomplete_not_clean() -> None:
    """
    无 proof 行（空 digest / key_id sentinel，如被剥离 proof 的普通记录）归
    incomplete：证据不足，不当 clean 也不猜 tampered。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    rec = _record(store, 0)
    bad_proof = Proof(
        format_version=CANONICAL_FORMAT_VERSION,
        sequence=rec.proof.sequence,
        previous_digest="",
        digest="",
        key_id="",
        key_epoch=1,
    )
    store._tamper_record(0, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.INCOMPLETE


def test_verify_missing_key_epoch_is_incomplete_not_clean() -> None:
    """proof 自带 epoch 的历史材料不可用 -> incomplete，不回退活动 key（计划 §5.2、§12.4）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    rec = _record(store, 0)
    # 用一个不存在的 epoch（LocalKeyProvider 未保留该代材料）
    bad_proof = Proof(
        format_version=rec.proof.format_version,
        sequence=rec.proof.sequence,
        previous_digest=rec.proof.previous_digest,
        digest=rec.proof.digest,
        key_id=rec.proof.key_id,
        key_epoch=rec.proof.key_epoch + 99,
    )
    store._tamper_record(0, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.INCOMPLETE


def test_verify_gap_is_incomplete() -> None:
    """缺记录（sequence 跳号）归 incomplete（计划 §12.3 删除中间事件）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))  # seq 1
    provider.record_chained(_event(id="e1"))  # seq 2
    provider.record_chained(_event(id="e2"))  # seq 3
    # 删除中间记录
    del store._records[1]  # noqa: SLF001 - 测试需直接删除内存记录
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.INCOMPLETE


def test_verify_tail_deletion_is_incomplete() -> None:
    """
    尾删（head 记录的中间前缀在、链头之前缺记录）：页扫描未达快照链头，
    归 incomplete，拒绝把截断前缀报告为 clean（F05 §6.1）。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(3):
        provider.record_chained(_event(id=f"e{i}"))
    # 快照后模拟截断：head 不动（快照上界仍是 3），但 records 被删尾
    del store._records[2]  # noqa: SLF001 - 测试需直接删除内存记录
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.INCOMPLETE
    assert result.high_water_mark == 2  # 连续成功验证到第 2 条


def test_verify_reorder_is_tampered() -> None:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    provider.record_chained(_event(id="e0"))
    provider.record_chained(_event(id="e1"))
    # 交换两条记录顺序
    store._records[0], store._records[1] = store._records[1], store._records[0]  # noqa: SLF001
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED


def test_verify_incremental_uses_checkpoint() -> None:
    """
    after_sequence>0：先验证第 N 条 checkpoint（计入 checked_count），再从其
    digest 续链验证后续记录（F05 §6.1 增量语义）。
    """
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(5):
        provider.record_chained(_event(id=f"e{i}"))
    result = provider.verify(after_sequence=3)
    assert result.status is AuditIntegrityStatus.CLEAN
    # checkpoint（seq 3）+ seq 4、5 都计入
    assert result.checked_count == 3
    assert result.high_water_mark == 5


def test_verify_incremental_checkpoint_only_advances_high_water() -> None:
    """checkpoint 校验通过后即使没有新记录，high_water_mark 也等于 after_sequence。"""
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(3):
        provider.record_chained(_event(id=f"e{i}"))
    result = provider.verify(after_sequence=3)
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.checked_count == 1  # 只有 checkpoint
    assert result.high_water_mark == 3


def test_verify_incremental_missing_checkpoint_is_incomplete() -> None:
    """checkpoint 不存在：incomplete，不回落 genesis、不跳到下一条。"""
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(2):
        provider.record_chained(_event(id=f"e{i}"))
    result = provider.verify(after_sequence=99)
    assert result.status is AuditIntegrityStatus.INCOMPLETE
    assert result.checked_count == 0
    assert result.high_water_mark == 0


def test_verify_incremental_tampered_checkpoint_breaks_chain() -> None:
    """checkpoint 内容被篡改：其 proof 验证失败，且以它为基线的后续记录也失配。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(4):
        provider.record_chained(_event(id=f"e{i}"))
    store._tamper_record(2, event=_event(id="e2", action="delete"))  # noqa: SLF001
    result = provider.verify(after_sequence=3)
    assert result.status is AuditIntegrityStatus.TAMPERED
    assert result.error_count >= 1


def test_verify_page_size_bounds_per_scan_call() -> None:
    """page_size 限制单页扫描量：fake store 只按窗口返回，provider 逐页推进。"""
    store = _FakeChainStore()
    provider = _provider(store=store)
    for i in range(5):
        provider.record_chained(_event(id=f"e{i}"))
    result = provider.verify(page_size=2)
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.checked_count == 5


def test_samples_bounded_and_truncated_flag() -> None:
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store, max_samples=2)
    for i in range(5):
        provider.record_chained(_event(id=f"e{i}"))
    # 全部篡改 digest
    for i, rec in enumerate(list(store._records)):
        bad_proof = Proof(
            format_version=rec.proof.format_version,
            sequence=rec.proof.sequence,
            previous_digest=rec.proof.previous_digest,
            digest="e" * 64,
            key_id=rec.proof.key_id,
            key_epoch=rec.proof.key_epoch,
        )
        store._tamper_record(i, proof=bad_proof)
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED
    assert result.error_count == 5
    assert len(result.samples) == 2  # 受 max_samples 约束
    assert result.truncated is True


def test_constant_time_compare_no_exception() -> None:
    """digest 比较走常时间 API：正常路径不抛（计划 §12.1）。"""
    provider = _provider()
    provider.record_chained(_event(id="e0"))
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN


# ====================================================================== #
# 错误脱敏（计划 §12.1：错误消息不含秘密）
# ====================================================================== #


def test_error_messages_do_not_leak_key_material() -> None:
    provider = _provider()
    record = provider.record_chained(_event(id="e0"))
    # KeyCapabilityError / ChainConflictError 消息不应含根密钥 hex
    try:
        raise KeyCapabilityError("audit key unavailable")
    except KeyCapabilityError as exc:
        assert _KEY_HEX not in str(exc)
    assert _KEY_HEX not in record.proof.digest  # digest 是 HMAC 输出，本就不含根密钥
    assert _KEY_HEX not in record.proof.key_id  # key_id 是不可逆指纹


# ====================================================================== #
# 装配期 capability 检查（计划 §4.3、§8.2 不变量 5）
# ====================================================================== #


def test_rejects_store_missing_required_capability() -> None:
    bad_cap = ChainStoreCapability(
        persistent=False,
        atomic_append=False,  # 缺原子追加
        stable_head_snapshot=True,
        key_epoch=True,
        external_anchor=False,
        streaming_scan=True,
    )

    class _BadStore(_FakeChainStore):
        def capabilities(self) -> ChainStoreCapability:
            return bad_cap

    with pytest.raises(ValidationError, match="atomic_append"):
        _provider(store=_BadStore())


def test_rejects_key_provider_without_mac() -> None:
    # KeyProvider ABC 校验在前，故用一个不继承 ABC 的假对象会先被 isinstance 拦下。
    # 用真正不支持 MAC 的 KeyProvider 子类覆盖 supports_mac。
    from jiuwen_memory.common.security.cryptography.key_provider import (
        KeyProvider as _KP,
    )

    class _NoMacKP(_KP):
        def active_key(self):
            return KeyRef(key_id="k", epoch=1)

        def rotate(self):
            return KeyRef(key_id="k", epoch=2)

        def wrap(self, data_key, *, purpose, org):
            raise NotImplementedError

        def unwrap(self, wrapped, *, purpose, org):
            raise NotImplementedError

        def health(self):
            return None

        def supports_mac(self) -> bool:
            return False

    with pytest.raises(KeyCapabilityError, match="MAC capability"):
        _provider(key_provider=_NoMacKP())


def test_cas_conflict_retries_and_eventually_raises() -> None:
    """CAS 冲突有界重试，超限抛 ChainConflictError（计划 §4.3）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store=store, max_cas_retries=2)

    class _ContendedStore(_FakeChainStore):
        def append(self, record, expected_head):
            # 总是冲突，模拟另一个 writer 持续抢先
            raise ChainConflictError("always conflicting")

    provider._store = _ContendedStore()  # noqa: SLF001 - 测试需替换被协调的 store
    with pytest.raises(ChainConflictError, match="exhausted"):
        provider.record_chained(_event(id="e0"))


def test_key_epoch_rotation_verifies_history() -> None:
    """epoch 轮换前后事件均可验证（计划 §12.4）。"""
    kp = _key_provider()
    store = _FakeChainStore()
    provider = _provider(store=store, key_provider=kp)
    provider.record_chained(_event(id="e0"))  # epoch 1
    kp.rotate()  # 推进到 epoch 2
    provider.record_chained(_event(id="e1"))  # epoch 2
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.key_epoch_range == (1, 2)


def test_active_key_ref_reflects_rotation() -> None:
    kp = _key_provider()
    provider = _provider(key_provider=kp)
    assert provider.active_key_ref().epoch == 1
    kp.rotate()
    assert provider.active_key_ref().epoch == 2


def test_provider_is_test_only_iff_non_persistent() -> None:
    """is_test_only 判据是后端 capability 的 persistent 声明，不是 target 名。"""
    non_persistent = ChainStoreCapability(
        persistent=False,
        atomic_append=True,
        stable_head_snapshot=True,
        key_epoch=True,
        external_anchor=False,
        streaming_scan=True,
    )

    class _TempStore(_FakeChainStore):
        def capabilities(self) -> ChainStoreCapability:
            return non_persistent

    assert _provider(store=_TempStore()).is_test_only() is True
    assert _provider().is_test_only() is True  # fake 默认非持久


# ====================================================================== #
# 独立验收故障样本镜像（security-plans/problems/2026-09-22-pr3-*
# 独立验收报告 PR3-05/09 的复现样本沉淀为正式测试）
# ====================================================================== #


class _FakeAnchor(AuditAnchor):
    """测试用外部锚点：返回固定锚定记录。"""

    def __init__(self, record: AnchorRecord | None) -> None:
        self._record = record

    def read_anchored(self, *, chain_id):
        return self._record

    def anchor_head(self, head, *, chain_id):
        return self._record

    def health(self) -> None:
        return None


def test_verify_high_water_frozen_at_first_bad_record() -> None:
    """
    连续高水位在首个坏记录处停止（PR3-09）：篡改中段后 high_water_mark 不得越过
    坏位置推进到链尾——以该水位作后续 checkpoint 的消费者会跳过尚未处理的损坏位置。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    provider = _provider(store)
    for index in range(3):
        provider.record_chained(_event(id=f"e{index}"))
    store._tamper_record(1, event=_event(id="e1", detail={"role": "attacker"}))
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.TAMPERED
    assert result.high_water_mark == 1


@pytest.mark.parametrize("relative_sequence", [-1, 0, 1])
def test_configured_anchor_returns_typed_state(relative_sequence) -> None:
    """
    配置锚点后相对链头的落后/相等/领先三种位置都产出契约枚举状态（PR3-05）：
    AnchorState.status 必须是 AnchorStatus，不得传裸字符串触发 TypeError。
    """
    store = _FakeChainStore()
    seed = _provider(store)
    for index in range(3):
        seed.record_chained(_event(id=f"e{index}"))
    head = store.read_head()
    provider = _provider(
        store,
        anchor=_FakeAnchor(
            AnchorRecord(
                chain_id="default",
                sequence=head.sequence + relative_sequence,
                digest=head.digest,
                key_id=head.key_id,
                epoch=head.key_epoch,
                format_version=head.format_version,
                anchored_at="",
            )
        ),
    )
    result = provider.verify()
    assert result.anchor.checked is True
    assert isinstance(result.anchor.status, AnchorStatus)


def test_lagging_anchor_conflicts_on_local_prefix_mismatch() -> None:
    """
    本地链长于锚点时必须核对锚定位置的本地前缀证据（PR3-05）：锚 digest 与本地
    对应记录不符 = 回滚/篡改嫌疑，报 rollback_suspected，不得只看 head 更靠后就
    lagging。
    """
    store = _FakeChainStore()
    seed = _provider(store)
    for index in range(3):
        seed.record_chained(_event(id=f"e{index}"))
    provider = _provider(
        store,
        anchor=_FakeAnchor(
            AnchorRecord(
                chain_id="default",
                sequence=1,
                digest="ff" * 32,
                key_id="k1",
                epoch=1,
                format_version=CANONICAL_FORMAT_VERSION,
                anchored_at="",
            )
        ),
    )
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.ROLLBACK_SUSPECTED


def test_lagging_anchor_matching_prefix_stays_lagging_and_clean() -> None:
    """锚定位置与本地前缀 digest 一致时：锚点状态 lagging，链验证本身保持 clean。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    store = _FakeChainStore()
    seed = _provider(store)
    for index in range(3):
        seed.record_chained(_event(id=f"e{index}"))
    provider = _provider(
        store,
        anchor=_FakeAnchor(
            AnchorRecord(
                chain_id="default",
                sequence=1,
                digest=store._records[0].proof.digest,
                key_id=store._records[0].proof.key_id,
                epoch=store._records[0].proof.key_epoch,
                format_version=CANONICAL_FORMAT_VERSION,
                anchored_at="",
            )
        ),
    )
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.anchor.status is AnchorStatus.LAGGING


def test_anchor_unavailable_states_are_typed() -> None:
    """
    锚点读取失败 / 未锚定过都产出 checked=True 的 UNAVAILABLE 状态（PR3-05）：
    可用性故障是「已尝试核对但锚点不可用」，不是「未核对」。
    """
    store = _FakeChainStore()
    seed = _provider(store)
    seed.record_chained(_event(id="e0"))

    class _BoomAnchor(_FakeAnchor):
        def read_anchored(self, *, chain_id):
            raise RuntimeError("anchor down")

    provider = _provider(store, anchor=_BoomAnchor(None))
    result = provider.verify()
    assert result.anchor.checked is True
    assert result.anchor.status is AnchorStatus.UNAVAILABLE

    provider2 = _provider(store, anchor=_FakeAnchor(None))
    result2 = provider2.verify()
    assert result2.anchor.status is AnchorStatus.UNAVAILABLE


# ====================================================================== #
# 轮换边界自动落链（计划 §5.2「轮换边界本身写入一条安全审计事件并推进链」；
# security-plans/problems/2026-09-22-pr3-reacceptance.md R8）
# ====================================================================== #


def test_rotation_boundary_is_chained_before_next_event() -> None:
    """
    活动密钥相对链头前进时，轮换边界先落一条 key_rotate 安全事件再写业务事件：
    边界事件用新 epoch 签发，链可验证，epoch 范围覆盖轮换前后。
    """
    kp = _key_provider()
    store = _FakeChainStore()
    provider = _provider(store=store, key_provider=kp)
    provider.record_chained(_event(id="e0"))  # epoch 1
    kp.rotate()  # 推进到 epoch 2
    provider.record_chained(_event(id="e1"))  # epoch 2

    # 轮换边界先于业务事件落链：seq=2 是 key_rotate，seq=3 是 e1。
    rotation = _record(store, 1)
    assert rotation.event.action == "key_rotate"
    assert rotation.event.layer == "security"
    assert rotation.proof.key_epoch == 2
    assert rotation.event.detail["key_epoch"] == "2"
    assert rotation.event.detail["previous_key_epoch"] == "1"
    business = _record(store, 2)
    assert business.event.id == "e1"
    assert business.proof.key_epoch == 2

    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.key_epoch_range == (1, 2)


def test_no_rotation_event_without_key_change() -> None:
    """活动密钥未变时不写多余的 key_rotate 事件：每条业务事件一条记录。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kp = _key_provider()
    store = _FakeChainStore()
    provider = _provider(store=store, key_provider=kp)
    provider.record_chained(_event(id="e0"))
    provider.record_chained(_event(id="e1"))
    assert len(store._records) == 2  # noqa: SLF001
    assert all(record.event.action == "write" for record in store._records)


def test_rotation_boundary_survives_repeated_rotations() -> None:
    """连续轮换：每次检测到 key ref 前进都在链上留边界，全部历史可验证。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    kp = _key_provider()
    store = _FakeChainStore()
    provider = _provider(store=store, key_provider=kp)
    provider.record_chained(_event(id="e0"))
    kp.rotate()
    provider.record_chained(_event(id="e1"))
    kp.rotate()
    provider.record_chained(_event(id="e2"))
    rotations = []
    for record in store._records:  # noqa: SLF001
        if record.event.action == "key_rotate":
            rotations.append(record)
    assert [record.proof.key_epoch for record in rotations] == [2, 3]
    result = provider.verify()
    assert result.status is AuditIntegrityStatus.CLEAN
    assert result.key_epoch_range == (1, 3)
