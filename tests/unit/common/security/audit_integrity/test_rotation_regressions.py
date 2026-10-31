"""审计签名期轮换、一致性与总工作量预算回归。"""

import pytest

from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from jiuwen_memory.common.audit.protected_audit_logger import ProtectedAuditLogger
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    ChainedHmacAuditIntegrityProvider,
)
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityStatus,
    KeyCapabilityError,
)
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import AuditEvent

pytestmark = pytest.mark.unit


@pytest.fixture(name="chain_factory")
def _chain_factory_fixture(tmp_path):
    stores = []

    def make(*, retries=10, key_retries=10):
        store = SqliteAuditLogger(str(tmp_path / f"audit-{len(stores)}.sqlite3"))
        stores.append(store)
        keys = LocalKeyProvider(key_hex="44" * 32)
        provider = ChainedHmacAuditIntegrityProvider(
            store, keys, max_cas_retries=retries, max_key_retries=key_retries
        )
        return store, keys, provider, ProtectedAuditLogger(provider, store)

    yield make
    for store in stores:
        store.close()


def test_rotation_during_mac_still_has_boundary_event(chain_factory, monkeypatch):
    store, keys, provider, wrapper = chain_factory()
    wrapper.record(AuditEvent(id="before", action="write"))
    original = keys.mac
    fired = False

    def rotating_mac(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            keys.rotate()
        return original(*args, **kwargs)

    monkeypatch.setattr(keys, "mac", rotating_mac)
    wrapper.record(AuditEvent(id="after", action="write"))
    assert provider.verify().status is AuditIntegrityStatus.CLEAN
    records = store.scan(after_sequence=0, through_sequence=store.read_head().sequence, limit=10)
    actual = [(r.event.action, r.proof.key_epoch) for r in records]
    assert [r.event.action for r in records] == ["write", "key_rotate", "write"], actual


def test_rotation_event_epoch_matches_actual_signature(chain_factory, monkeypatch):
    store, keys, provider, wrapper = chain_factory()
    wrapper.record(AuditEvent(id="before", action="write"))
    keys.rotate()
    original = keys.mac
    fired = False

    def rotating_mac(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            keys.rotate()
        return original(*args, **kwargs)

    monkeypatch.setattr(keys, "mac", rotating_mac)
    wrapper.record(AuditEvent(id="after", action="write"))
    assert provider.verify().status is AuditIntegrityStatus.CLEAN
    records = store.scan(after_sequence=0, through_sequence=store.read_head().sequence, limit=10)
    rotation = next(r for r in records if r.event.action == "key_rotate")
    assert rotation.event.detail["key_epoch"] == str(rotation.proof.key_epoch), (
        rotation.event.detail,
        rotation.proof.key_epoch,
    )


def test_rotation_metadata_stays_consistent_across_two_signing_rotations(
    chain_factory, monkeypatch
):
    store, keys, provider, wrapper = chain_factory()
    wrapper.record(AuditEvent(id="before", action="write"))
    keys.rotate()
    original_mac = keys.mac
    calls = 0

    def rotate_on_boundary_signatures(*args, **kwargs):
        nonlocal calls
        calls += 1
        # 第 1 次为业务预签；第 2 次为轮换事件首次签发，第 4 次为修正 detail 后重签。
        # 使用真实 rotate/mac，仅控制合法并发操作的发生时机。
        if calls in (2, 4):
            keys.rotate()
        return original_mac(*args, **kwargs)

    monkeypatch.setattr(keys, "mac", rotate_on_boundary_signatures)
    wrapper.record(AuditEvent(id="after", action="write"))
    assert calls >= 4
    assert provider.verify().status is AuditIntegrityStatus.CLEAN
    records = store.scan(0, 10, through_sequence=store.read_head().sequence)
    rotation = next(record for record in records if record.event.action == "key_rotate")
    assert rotation.event.detail["key_epoch"] == str(rotation.proof.key_epoch), (
        rotation.event.detail,
        rotation.proof.key_epoch,
    )


def test_normal_rotation_succeeds_with_one_cas_attempt(chain_factory):
    store, keys, provider, wrapper = chain_factory(retries=1)
    wrapper.record(AuditEvent(id="before", action="write"))
    keys.rotate()
    # 无竞争、无故障、仅一次正常轮换；不能把成功追加边界算作 CAS 冲突耗尽。
    wrapper.record(AuditEvent(id="after", action="write"))
    assert provider.verify().status is AuditIntegrityStatus.CLEAN
    assert not wrapper.integrity_degraded
    assert [r.event.action for r in store.scan(0, 10, through_sequence=3)] == [
        "write",
        "key_rotate",
        "write",
    ]


def test_continuous_rotation_has_a_total_work_budget(chain_factory, monkeypatch):
    store, keys, provider, wrapper = chain_factory(retries=1, key_retries=2)
    wrapper.record(AuditEvent(id="before", action="write"))
    keys.rotate()
    original_append = store.append
    rotations = 0

    def append_then_rotate(record, expected_head):
        nonlocal rotations
        if record.event.action == "key_rotate":
            rotations += 1
            if rotations > 32:
                # 测试保护阈值，不是新产品契约；防止实际实现无限追加造成测试挂死。
                raise RuntimeError("watchdog: 33 rotation appends without budget exhaustion")
        head = original_append(record, expected_head)
        if record.event.action == "key_rotate":
            keys.rotate()
        return head

    monkeypatch.setattr(store, "append", append_then_rotate)
    with pytest.raises(KeyCapabilityError):
        provider.record_chained(AuditEvent(id="after", action="write"))
