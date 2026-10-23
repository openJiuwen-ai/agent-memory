# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""阶段 B：SQLite ChainStore capability 的原子性、持久化与 schema 边界（计划 §7、§12.2）。

无生产库待迁移（计划 §16）：覆盖空库 genesis、旧无签名库拒绝、合法现库续链、损坏 schema
拒绝、多连接/多实例并发不分叉。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from jiuwen_memory.common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    ChainedHmacAuditIntegrityProvider,
)
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditMigrationRequiredError,
    AuditSchemaError,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import GENESIS_DIGEST
from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
    LocalKeyProvider,
)
from jiuwen_memory.common.type_def import AuditEvent
from jiuwen_memory.common.type_def.scope import Scope

pytestmark = pytest.mark.unit

_KEY_HEX = "44" * 32


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


def _provider(store: SqliteAuditLogger) -> ChainedHmacAuditIntegrityProvider:
    return ChainedHmacAuditIntegrityProvider(store, _kp())


def test_empty_db_creates_genesis(tmp_path) -> None:
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    # 首次操作触发完整性 schema 建立
    provider.record_chained(_event("a"))
    head = store.read_head()
    assert head.sequence == 1
    assert head.digest != GENESIS_DIGEST
    assert provider.verify().status.value == "clean"
    store.close()


def test_unsigned_db_with_events_rejected(tmp_path) -> None:
    """旧无签名库（有事件、user_version<2）拒绝启动（计划 §7.2.3）。"""
    db = tmp_path / "audit.sqlite3"
    plain = SqliteAuditLogger(str(db))
    plain.record(_event("plain-1"))  # 普通模式写入无 proof 事件
    plain.close()
    # 同一库启用完整性
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    with pytest.raises(AuditMigrationRequiredError):
        provider.record_chained(_event("a"))
    store.close()


def test_restart_continues_chain(tmp_path) -> None:
    """重启从稳定链头续链，不从空链重新开始（计划 §1.3 经验 2、§12.2）。"""
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for i in range(5):
        provider.record_chained(_event(f"e{i}"))
    head_before = store.read_head()
    store.close()

    # 重新打开同一库
    store2 = SqliteAuditLogger(str(db))
    provider2 = _provider(store2)
    head_after = store2.read_head()
    assert head_after == head_before  # 续链
    rec = provider2.record_chained(_event("e5"))
    assert rec.proof.sequence == 6  # 从 6 继续，不是从 1
    result = provider2.verify()
    assert result.status.value == "clean"
    assert result.checked_count == 6
    store2.close()


def test_corrupted_head_rejected(tmp_path) -> None:
    """head 与末事件不一致 -> AuditSchemaError（计划 §7.2.5、§1.3 经验 3）。"""
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.record_chained(_event("a"))
    store.close()
    # 直接篡改 head digest
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE audit_chain_head SET digest = 'ab' * 32 WHERE id = 1")
    conn.commit()
    conn.close()
    store2 = SqliteAuditLogger(str(db))
    provider2 = _provider(store2)
    with pytest.raises(AuditSchemaError):
        provider2.record_chained(_event("b"))
    store2.close()


def test_missing_proof_column_rejected(tmp_path) -> None:
    """user_version=2 但缺 proof 列 -> AuditSchemaError（计划 §7.2.5）。"""
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.record_chained(_event("a"))
    store.close()
    import sqlite3

    conn = sqlite3.connect(str(db))
    # 重建无 proof 列的表但保留 user_version=2，模拟损坏
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE audit_events_no_proof AS
            SELECT seq, id, actor_org, actor_space, actor_user, actor_agent, actor_session,
                   target_org, target_space, target_user, target_agent, target_session,
                   action, target_id, layer, decision, occurred_at, detail_json
            FROM audit_events;
        DROP TABLE audit_events;
        ALTER TABLE audit_events_no_proof RENAME TO audit_events;
        COMMIT;
        """
    )
    conn.close()
    store2 = SqliteAuditLogger(str(db))
    provider2 = _provider(store2)
    with pytest.raises(AuditSchemaError):
        provider2.record_chained(_event("b"))
    store2.close()


def test_two_connections_concurrent_no_fork(tmp_path) -> None:
    """两个连接（两个实例）并发写同一文件不分叉（计划 §1.3 经验 1、§12.2）。"""
    db = tmp_path / "audit.sqlite3"
    store1 = SqliteAuditLogger(str(db))
    store2 = SqliteAuditLogger(str(db))
    # 先用 store1 建立完整性 schema
    provider1 = _provider(store1)
    provider1.record_chained(_event("seed"))
    provider2 = _provider(store2)
    n = 80
    errors: list[Exception] = []

    def writer(p, start: int) -> None:
        try:
            for i in range(start, start + n):
                p.record_chained(_event(f"w-{start}-{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(provider1, 0))
    t2 = threading.Thread(target=writer, args=(provider2, n))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    head = store1.read_head()
    assert head.sequence == 1 + 2 * n
    result = provider1.verify()
    assert result.status.value == "clean"
    assert result.checked_count == 1 + 2 * n
    store1.close()
    store2.close()


def test_cas_conflict_is_retryable_across_connections(tmp_path) -> None:
    """CAS 冲突由 provider 有界重试吸收（计划 §4.3、§12.2）。"""
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    # 4 线程高压竞争，提高重试上限以吸收调度抖动（生产默认 10 适用于低竞争审计路径）。
    provider = ChainedHmacAuditIntegrityProvider(store, _kp(), max_cas_retries=200)
    # 并发多线程经同一 provider（同一 store 实例）写，CAS 冲突由重试吸收
    n = 60
    threads = []
    errors: list[Exception] = []

    def writer(start: int) -> None:
        try:
            for i in range(start, start + n):
                provider.record_chained(_event(f"t-{start}-{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    for t in range(4):
        threads.append(threading.Thread(target=writer, args=(t * n,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.read_head().sequence == 4 * n
    store.close()


def test_scan_uses_keyset_no_offset(tmp_path) -> None:
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for i in range(15):
        provider.record_chained(_event(f"e{i}"))
    seen: list[int] = []
    after = 0
    while True:
        page = store.scan(after, 4, through_sequence=store.read_head().sequence)
        if not page:
            break
        for r in page:
            seen.append(r.proof.sequence)
        after = page[-1].proof.sequence
    assert seen == list(range(1, 16))
    store.close()


def test_query_returns_events_without_proof(tmp_path) -> None:
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.record_chained(_event("a"))
    provider.record_chained(_event("b"))
    events = store.query({}, limit=100)
    assert [e.id for e in events] == ["a", "b"]
    # proof 不在普通查询结果里
    assert all(not hasattr(e, "proof") for e in events)
    store.close()


def test_null_proof_row_reads_as_sentinel_and_verifies_incomplete(tmp_path) -> None:
    """
    proof 列被置 NULL（普通记录混入完整性库 / proof 被剥离）经 scan 读出 sentinel
    proof（契约：proof 列可空）；中段剥离同时打断后续链接，验证拒绝当 clean，连续
    高水位在首个坏记录处冻结（PR3-09）。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    # 中段记录的 proof 被剥离：digest/proof_format_version 置 NULL。
    # （末行 proof 剥离属于 head/末行不一致，由 snapshot 一致性核对拒绝——
    # 见 test_head_to_last_row_proof_stripped_raises_schema_error。）
    store._conn.execute(  # noqa: SLF001 - 测试需绕过链直接篡改 proof 列
        "UPDATE audit_events SET digest = NULL, proof_format_version = NULL WHERE seq = 2"
    )
    store._conn.commit()
    page = store.scan(0, 10, through_sequence=store.read_head().sequence)
    assert len(page) == 3
    assert page[1].proof.digest == ""  # sentinel：NULL-proof
    assert page[1].proof.key_id == ""
    result = provider.verify()
    # seq=2 sentinel 归 incomplete，seq=3 的链接校验（previous_digest 对不上被剥离
    # 的前序）归 tampered；聚合以 tampered 优先——剥离中段 proof 本就是篡改。
    assert result.status.value == "tampered"
    assert result.error_count == 2
    assert result.high_water_mark == 1  # 首个坏记录处冻结连续高水位（PR3-09）
    store.close()


def test_head_to_last_row_proof_stripped_raises_schema_error(tmp_path) -> None:
    """
    末行 proof 被剥离：head 声称的 digest 在真实末行找不到对应证据，稳定快照的
    head/末行一致性核对直接拒绝（PR3-01），不把伪造 head 的窗口当验证结果。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    store._conn.execute(  # noqa: SLF001 - 测试需绕过链直接篡改末行 proof
        "UPDATE audit_events SET digest = NULL WHERE seq = 3"
    )
    store._conn.commit()
    with pytest.raises(AuditSchemaError):
        provider.verify()
    store.close()


# ====================================================================== #
# 独立验收故障样本镜像（security-plans/problems/2026-09-22-pr3-*
# 独立验收报告 PR3-01/02/03/04 的复现样本沉淀为正式测试）
# ====================================================================== #


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE audit_chain_head SET digest = 'forged'",
        "UPDATE audit_chain_head SET sequence = 1",
        "UPDATE audit_chain_head SET sequence = 0",
    ],
)
def test_forged_head_rejected_at_runtime(tmp_path, mutation) -> None:
    """
    运行期伪造 head（不删除、不重签任何事件）不得把验证窗口缩小成 clean（PR3-01）：
    稳定快照在同一事务内核对 head 与真实末行 / genesis 条件，任一不符即拒绝。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    store._conn.execute(mutation)  # noqa: SLF001 - 攻击注入需绕过链直接篡改 head
    store._conn.commit()
    with pytest.raises(AuditSchemaError):
        provider.verify()
    store.close()


def test_health_rechecks_live_chain_consistency(tmp_path) -> None:
    """
    health() 每次真实核对 head 与末事件，不被初始化缓存代替（PR3-01）：链尾被删后
    运行期探测必须失败。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    store._conn.execute(  # noqa: SLF001 - 故障注入需绕过链直接删链尾
        "DELETE FROM audit_events WHERE seq = 3"
    )
    store._conn.commit()
    with pytest.raises(AuditSchemaError):
        provider.health()
    store.close()


def test_reopen_rejects_future_user_version(tmp_path) -> None:
    """user_version 大于当前构建理解的完整性版本时拒绝打开，不当作版本 2 放行（PR3-03）。"""
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.record_chained(_event("a"))
    store.close()

    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    store2 = SqliteAuditLogger(str(db))
    with pytest.raises(AuditSchemaError):
        store2.health()
    store2.close()


@pytest.mark.parametrize("column", ["actor_space", "target_org", "detail_json"])
def test_reopen_rejects_missing_core_column_without_repair(tmp_path, column) -> None:
    """
    完整性库缺核心事件列（scope 列、detail_json）时禁止初始化 DDL 修补，重开直接
    拒绝（PR3-03）：核对范围是全部核心列，不只见 proof 列。
    """
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.record_chained(_event("a"))
    store.close()

    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(f"ALTER TABLE audit_events DROP COLUMN {column}")
    conn.commit()
    conn.close()

    store2 = SqliteAuditLogger(str(db))
    with pytest.raises(AuditSchemaError):
        store2.health()
    store2.close()


def test_memory_db_declares_non_persistent() -> None:
    """
    :memory: 链按真实存储形态声明非持久（PR3-04）：重启即丢链的存储不得因类是
    SQLite 就声明 persistent，否则绕过「非持久链禁止生产」的装配判断。
    """
    store = SqliteAuditLogger(":memory:")
    provider = _provider(store)
    try:
        assert not store.capabilities().persistent
        assert provider.is_test_only()
    finally:
        store.close()


def test_key_epoch_null_row_is_not_clean(tmp_path) -> None:
    """
    key_epoch 被置 NULL 时不得在反序列化中修补成默认代次（PR3-02）：该行读出
    sentinel，验证拒绝当 clean。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    store._conn.execute(  # noqa: SLF001 - 故障注入需绕过链直接篡改 epoch
        "UPDATE audit_events SET key_epoch = NULL WHERE seq = 2"
    )
    store._conn.commit()
    result = provider.verify()
    assert result.status.value != "clean"
    assert result.high_water_mark == 1  # 首个坏记录处冻结连续高水位（PR3-09）
    store.close()


# ====================================================================== #
# 复验故障样本镜像（security-plans/problems/2026-09-22-pr3-reacceptance.md
# R2/R4/R5/R6 的复现样本沉淀为正式测试）
# ====================================================================== #


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE audit_chain_head SET key_id = 'forged'",
        "UPDATE audit_chain_head SET key_epoch = 99",
    ],
)
def test_full_head_metadata_is_verified(tmp_path, mutation) -> None:
    """
    head 与末 proof 的完整 key ref 必须逐字段核对（R4）：只比 sequence/digest 会放过
    「伪造成另一把密钥签的 head」，链头元数据不完整即拒绝。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    store._conn.execute(mutation)  # noqa: SLF001 - 攻击注入需绕过链直接篡改 head
    store._conn.commit()
    with pytest.raises(AuditSchemaError):
        provider.verify()
    with pytest.raises(AuditSchemaError):
        provider.health()
    store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE audit_chain_head SET digest = 'forged'",
        "UPDATE audit_chain_head SET key_id = 'forged'",
        "UPDATE audit_chain_head SET key_epoch = 7",
        "UPDATE audit_chain_head SET sequence = 1",
    ],
)
def test_empty_genesis_fixed_values_are_verified(tmp_path, mutation) -> None:
    """
    空链的 genesis head 携带固定值（R4）：digest 必须是 GENESIS_DIGEST、key ref 为空、
    sequence 为 0；篡改任一项都不得放行为 clean。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    provider.health()  # 建立完整性 schema 与 genesis head
    store._conn.execute(mutation)  # noqa: SLF001 - 攻击注入需绕过链直接篡改 genesis
    store._conn.commit()
    with pytest.raises(AuditSchemaError):
        provider.verify()
    with pytest.raises(AuditSchemaError):
        provider.health()
    store.close()


def test_live_schema_version_is_rechecked(tmp_path) -> None:
    """
    运行期 health 复核 schema 版本（R5）：初始化结果可缓存，健康核验不可——初始化后
    user_version 被改成未知版本是运行期损坏，不得被 ``_integrity_ready`` 缓存放行。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b"):
        provider.record_chained(_event(label))
    store._conn.execute("PRAGMA user_version = 99")  # noqa: SLF001 - 故障注入
    with pytest.raises(AuditSchemaError):
        provider.health()
    store.close()


def test_temporary_sqlite_database_is_not_persistent() -> None:
    """
    空路径的 SQLite 是临时数据库（R6）：连接关闭即丢链，必须声明非持久；不能以
    「不是 :memory:」等同持久文件数据库，否则绕过生产门控。
    """
    store = SqliteAuditLogger("")
    provider = _provider(store)
    try:
        assert not store.capabilities().persistent
        assert provider.is_test_only()
    finally:
        store.close()


def test_health_and_append_share_the_connection_lock(tmp_path) -> None:
    """
    health 的 BEGIN/COMMIT 与 append 共享实例锁（R2）：同一连接的两个合法操作交错
    时，追加不得在 health 的事务内嵌套 BEGIN IMMEDIATE 而耗尽 CAS 重试。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b"):
        provider.record_chained(_event(label))

    began = threading.Event()
    release = threading.Event()
    connection = store._conn  # noqa: SLF001 - 并发注入需替换连接包装

    class _PausedConnection:
        def __init__(self, wrapped):
            self._connection = wrapped

        def execute(self, sql, *args):
            result = self._connection.execute(sql, *args)
            if sql == "BEGIN":
                began.set()
                assert release.wait(5), "test coordination timed out"
            return result

        def __getattr__(self, name):
            return getattr(self._connection, name)

    store._conn = _PausedConnection(connection)  # noqa: SLF001
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            health = pool.submit(provider.health)
            assert began.wait(5), "health did not start its transaction"
            writer = pool.submit(provider.record_chained, _event("concurrent"))
            try:
                writer.result(timeout=0.2)  # 共享锁时 writer 应等待 health 提交
            except TimeoutError:
                pass
            finally:
                release.set()
                health.result(timeout=5)
            writer.result(timeout=5)  # health 提交后追加必须成功
    finally:
        store._conn = connection  # noqa: SLF001
        store.close()


def test_health_and_verify_share_the_connection_lock(tmp_path) -> None:
    """
    health 与 verify 并发（R2 回归）：verify 的 snapshot/scan 事务与 health 的核对
    事务在同一连接上交错，两侧都必须正常完成、不互相误提交/回滚。
    """
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "audit.sqlite3"
    store = SqliteAuditLogger(str(db))
    provider = _provider(store)
    for label in ("a", "b", "c"):
        provider.record_chained(_event(label))
    with ThreadPoolExecutor(max_workers=4) as pool:
        healths = [pool.submit(provider.health) for _ in range(4)]
        verifies = [pool.submit(provider.verify) for _ in range(4)]
        for task in healths:
            task.result(timeout=10)
        for task in verifies:
            assert task.result(timeout=10).status.value == "clean"
    store.close()
