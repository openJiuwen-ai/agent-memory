# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""双后端 head/genesis 一致性 conformance（计划 §7.1；R4 修复标准）。

同一组篡改场景对 SQLite 与内存两种后端各跑一遍：head 与末记录逐字段一致、genesis
携带固定值是 :class:`ChainedAuditStore` 的固定契约，不能只修具体数据库中的旧样本——
provider 对稳定快照的统一复核（genesis 预检 + clean 扫描后的 head 复核）与各后端
自检都须遵守。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger
from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    ChainedHmacAuditIntegrityProvider,
)
from jiuwen_memory.common.security.audit_integrity.base import AuditSchemaError
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import AuditEvent
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit

_KEY_HEX = "55" * 32


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


def _sqlite_store(tmp_path):
    return SqliteAuditLogger(str(tmp_path / "conformance.sqlite3"))


@pytest.fixture(name="backend", params=["sqlite", "memory"])
def _backend_fixture(request, tmp_path):
    """同一 provider 装配下两种后端：SQLite 用完关连接，内存后端无资源。"""
    if request.param == "sqlite":
        store = _sqlite_store(tmp_path)
        request.addfinalizer(store.close)
    else:
        store = InMemoryAuditLogger()
    provider = ChainedHmacAuditIntegrityProvider(
        store, LocalKeyProvider(key_hex=_KEY_HEX, create_key_file=False)
    )
    return store, provider


def _tamper_head(store, **overrides):
    """按后端各自持有 head 的方式篡改给定字段（测试专用攻击注入）。"""
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    if isinstance(store, InMemoryAuditLogger):
        head = store._chain_head  # noqa: SLF001
        store._chain_head = type(head)(  # noqa: SLF001
            sequence=overrides.get("sequence", head.sequence),
            digest=overrides.get("digest", head.digest),
            key_id=overrides.get("key_id", head.key_id),
            key_epoch=overrides.get("key_epoch", head.key_epoch),
            format_version=overrides.get("format_version", head.format_version),
        )
        return
    sets = []
    if "sequence" in overrides:
        sets.append(f"sequence = {overrides['sequence']!r}")
    if "digest" in overrides:
        sets.append(f"digest = {overrides['digest']!r}")
    if "key_id" in overrides:
        sets.append(f"key_id = {overrides['key_id']!r}")
    if "key_epoch" in overrides:
        sets.append(f"key_epoch = {overrides['key_epoch']!r}")
    if "format_version" in overrides:
        sets.append(f"format_version = {overrides['format_version']!r}")
    store._conn.execute(  # noqa: SLF001 - 攻击注入需绕过链直接篡改 head
        f"UPDATE audit_chain_head SET {', '.join(sets)} WHERE id = 1"
    )
    store._conn.commit()  # noqa: SLF001


@pytest.mark.parametrize(
    "overrides",
    [
        {"digest": "ff" * 32},
        {"key_id": "forged"},
        {"key_epoch": 99},
        {"sequence": 0},
        {"format_version": 7},
    ],
)
def test_forged_head_rejected_on_both_backends(backend, overrides) -> None:
    """
    非空链的 head 任一字段被篡改都拒绝（R4）：双后端同一条契约，不放过只改
    key_id/key_epoch/format_version 这类旧样本之外的相邻路径。
    """
    store, provider = backend
    for label in ("a", "b"):
        provider.record_chained(_event(label))
    _tamper_head(store, **overrides)
    with pytest.raises(AuditSchemaError):
        provider.verify()
    with pytest.raises(AuditSchemaError):
        provider.health()


@pytest.mark.parametrize(
    "overrides",
    [
        {"digest": "ff" * 32},
        {"key_id": "forged"},
        {"key_epoch": 7},
        {"sequence": 1},
        {"format_version": 7},
    ],
)
def test_genesis_fixed_values_enforced_on_both_backends(backend, overrides) -> None:
    """
    空链 genesis head 携带固定值（R4）：digest=GENESIS、空 key ref、sequence=0、
    当前格式版本；篡改任一项在 verify 与 health 都拒绝。
    """
    store, provider = backend
    provider.health()
    _tamper_head(store, **overrides)
    with pytest.raises(AuditSchemaError):
        provider.verify()
    with pytest.raises(AuditSchemaError):
        provider.health()


def test_healthy_chain_verifies_clean_on_both_backends(backend) -> None:
    """conformance 反向样本：未篡改的链在两种后端都 clean，固定契约不误报。"""
    store, provider = backend
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    provider.health()
    result = provider.verify()
    assert result.status.value == "clean"
    assert result.high_water_mark == 3
