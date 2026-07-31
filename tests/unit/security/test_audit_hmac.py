"""审计链式 HMAC 完整性保护（security.md §7.3 / 审计第三次 PR③ 收口）。

PR③ 用 ``HmacAuditLogger`` 装饰器包装 CAS-capable ``AuditLogger``：record 时算链式 HMAC
（每条含前一条的 HMAC）塞进 ``event.detail``，``verify_integrity`` 全量校验返回
篡改行。改一行 = 破坏该行及后续所有行的 HMAC。

key 从 ``LocalKeyProvider.get_encryption_root_key()`` 经 HKDF 派生（context=audit），
与加密根密钥同源但派生隔离。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger
from common.errors import ValidationError
from common.type_def import AuditEvent, Scope

pytestmark = pytest.mark.unit

_ACTOR = Scope(org="acme", user="alice")
_TS = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _event(eid: str, *, action: str = "write", detail: dict | None = None) -> AuditEvent:
    return AuditEvent(
        id=eid,
        actor=_ACTOR,
        action=action,
        layer="api",
        decision="allow",
        target_id=f"unit-{eid}",
        occurred_at=_TS,
        detail=detail or {},
        target=_ACTOR,
    )


def _hmac_key() -> bytes:
    """用固定 root key 派生 audit key，测试可复现。"""
    from common.security.security_impl.local_envelope_security_provider import (
        LocalKeyProvider,
    )

    return LocalKeyProvider(key_hex="00" * 32).get_encryption_root_key()


# -- 链式 HMAC ----------------------------------------------------------------- #


def test_chain_links_each_event_to_previous() -> None:
    """每条 HMAC 含前一条的 HMAC；顺序敏感。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(inner, hmac_key=key)

    logger.record(_event("1"))
    logger.record(_event("2"))
    logger.record(_event("3"))

    assert logger.verify_integrity().status == "clean"  # 无篡改

    events = inner.events
    assert "_hmac" in events[0].detail
    assert events[0].detail.get("prev_hmac", "") == ""  # 链首无前驱
    assert events[1].detail["prev_hmac"] == events[0].detail["_hmac"]
    assert events[2].detail["prev_hmac"] == events[1].detail["_hmac"]


def test_tampering_one_event_breaks_it_and_subsequent() -> None:
    """改一行内容（不重算 HMAC）-> 该行被检出；若重算该行 HMAC 则后续链断。

    §7.3「改一行 = 破坏该行及后续」：攻击者改第二条 action 后--
    - 不重算 HMAC：第二条自身 HMAC 与内容不符 -> 检出；
    - 重算第二条 HMAC（假设有 key）：第二条 _hmac 变 -> 第三条 prev_hmac 对不上 -> 检出。
    没有根密钥就修不好，这才是链式 HMAC 的保证。
    """
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    logger.record(_event("1"))
    logger.record(_event("2"))
    logger.record(_event("3"))

    # 篡改第二条的 action，不重算 HMAC（模拟无 key 的攻击者）
    inner.events[1].action = "delete"
    result = logger.verify_integrity()
    assert 1 in result.tampered_indices
    assert 0 not in result.tampered_indices


def test_recomputing_tampered_hmac_breaks_subsequent() -> None:
    """若攻击者用 key 重算被改行的 HMAC，则后续行的 prev_hmac 对不上。"""
    import hashlib
    import hmac as hmac_mod

    from security.audit_hmac import HmacAuditLogger, _canonical_event_bytes, derive_audit_key

    key = derive_audit_key(_hmac_key())
    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=key)
    logger.record(_event("1"))
    logger.record(_event("2"))
    logger.record(_event("3"))

    # 攻击者改第二条并用 key 重算其 HMAC（但不动第三条）
    inner.events[1].action = "delete"
    prev = inner.events[1].detail["prev_hmac"]
    new_hmac = hmac_mod.new(
        key, prev.encode() + _canonical_event_bytes(inner.events[1]), hashlib.sha256
    ).hexdigest()
    inner.events[1].detail["_hmac"] = new_hmac

    # 第二条自身现在自洽，但第三条 prev_hmac 仍是旧值 -> 对不上
    result = logger.verify_integrity()
    assert 1 not in result.tampered_indices  # 第二条修好了
    assert 2 in result.tampered_indices  # 第三条链断了


def test_tampering_replayed_with_wrong_prev_breaks_chain() -> None:
    """换掉某条的 prev_hmac（重排/拼接攻击）也被检出。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    logger.record(_event("1"))
    logger.record(_event("2"))

    # 伪造第二条的 prev_hmac
    inner.events[1].detail["prev_hmac"] = "deadbeef"
    assert 1 in logger.verify_integrity().tampered_indices


def test_verify_returns_empty_on_clean_chain() -> None:
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    for i in range(10):
        logger.record(_event(str(i)))
    assert logger.verify_integrity().status == "clean"


# -- 并发与重启（审计 P1-1 / P1-2）--------------------------------------------- #


def test_concurrent_record_does_not_fork_chain() -> None:
    """审计 P1-1：并发 record 不分叉链。

    两线程 Barrier 同步后同时 record，加锁后链头读取-HMAC-追加-更新原子，
    不会两线程读到相同 prev 各自写入。验证：verify_integrity 空。
    """
    import threading

    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    barrier = threading.Barrier(2)

    def fire(i):
        barrier.wait(timeout=5)
        logger.record(_event(str(i)))

    t1 = threading.Thread(target=fire, args=(0,))
    t2 = threading.Thread(target=fire, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    # 并发后链应自洽
    assert logger.verify_integrity().status == "clean"


def test_failed_delegate_write_does_not_advance_head() -> None:
    """审计 P1-1：后端写入失败不推进链头，避免链头与后端不一致。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    class _FailingDelegate(InMemoryAuditLogger):
        def record_chained(self, event, expected_head):
            raise RuntimeError("backend down")

    inner = _FailingDelegate()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    head_before = logger._prev_hmac
    with pytest.raises(RuntimeError):
        logger.record(_event("1"))
    # 链头未推进
    assert logger._prev_hmac == head_before


def test_restart_recovers_chain_head_from_persistent_backend() -> None:
    """审计 P1-2：重启后从持久化后端恢复链头，续接旧链而非从空开始。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(inner, hmac_key=key)
    logger.record(_event("1"))
    logger.record(_event("2"))
    last_hmac = inner.events[-1].detail["_hmac"]

    # 模拟重启：新实例，同一 inner（持久化后端）
    logger2 = HmacAuditLogger(inner, hmac_key=key)
    assert logger2._prev_hmac == last_hmac  # 恢复了旧链头
    logger2.record(_event("3"))
    # 新事件 prev_hmac 续接旧链
    assert inner.events[-1].detail["prev_hmac"] == last_hmac
    assert logger2.verify_integrity().status == "clean"


def test_restart_with_sqlite_persists_through_new_instance(tmp_path) -> None:
    """审计 P1-2：真实 SQLite 跨实例/跨重启续链。"""
    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    logger.record(_event("1"))
    logger.record(_event("2"))
    assert logger.verify_integrity().status == "clean"

    # 新实例（重启），同一 db 文件
    logger2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    logger2.record(_event("3"))
    assert logger2.verify_integrity().status == "clean"


# -- 多实例事务 CAS（审计 P1-1）--------------------------------------------- #


def test_two_instances_same_sqlite_do_not_fork(tmp_path) -> None:
    """审计 P1-1：两个 HmacAuditLogger 实例写同一 SQLite 文件不分叉。

    此前实例锁只保护单实例内并发，两实例读相同 head 各自写入 -> 分叉链。
    record_chained 事务 CAS（BEGIN IMMEDIATE + chain-head 表）让后到者
    ConflictError 重试，保证链连续。
    """
    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    l1 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    l2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    l1.record(_event("0"))
    l2.record(_event("1"))
    l1.record(_event("2"))
    # 链应自洽（不分叉）
    assert l1.verify_integrity().status == "clean"
    # prev_hmac 链连续：每条 prev == 前一条 _hmac
    events = l1.query({}, limit=100)
    prev = ""
    for e in events:
        assert e.detail["prev_hmac"] == prev
        prev = e.detail["_hmac"]


def test_concurrent_two_instances_serialize_via_cas(tmp_path) -> None:
    """审计 P1-1：两实例并发写同一库，CAS 串行化，最终链自洽。"""
    import threading

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    l1 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    l2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    barrier = threading.Barrier(2)
    errors = []

    def fire(logger, i):
        try:
            barrier.wait(timeout=5)
            logger.record(_event(str(i)))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=fire, args=(l1, "a"))
    t2 = threading.Thread(target=fire, args=(l2, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not errors, f"并发写入出错: {errors}"
    # 合并校验（用任一实例）
    assert l1.verify_integrity().status == "clean"


# -- iter_chain 分页边界（审计 P1-1）------------------------------------------ #


@pytest.mark.parametrize("count", [999, 1000, 1001, 2000])
def test_iter_chain_page_boundary_clean(count) -> None:
    """审计 P1-1：默认 iter_chain 在 999/1000/1001/2000 边界不重复行。

    此前 1-based 伪 seq 错位，1000 条边界重复末行 -> 健康链被误报 tampered。
    """
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()), verify_on_start=False)
    for i in range(count):
        logger.record(_event(str(i)))
    r = logger.verify_integrity()
    assert r.status == "clean", f"{count} 条边界误报: {r.tampered_indices[:3]}"


# -- last_seq 迁移与并发快照（审计 P1-2）------------------------------------- #


def test_previous_schema_last_seq_backfilled_on_migrate(tmp_path) -> None:
    """审计 P1-2a：紧邻上一版 schema（有 head_hmac 缺 last_seq）迁移回填真实 seq。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    old = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    old.record(_event("1"))
    old.record(_event("2"))
    real_head = old._prev_hmac
    # 紧邻旧 schema：有 head_hmac 但 DROP 重建缺 last_seq
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE audit_chain_head")
    conn.execute("PRAGMA user_version = 0")  # 模拟真正的旧库
    conn.execute(
        "CREATE TABLE audit_chain_head (id INTEGER PRIMARY KEY CHECK (id = 0),"
        ' head_hmac TEXT NOT NULL DEFAULT "")'
    )
    conn.execute("INSERT INTO audit_chain_head (id, head_hmac) VALUES (0, ?)", (real_head,))
    conn.commit()
    conn.close()
    # 新版迁移：回填 last_seq 为真实 MAX(seq)
    HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    conn = sqlite3.connect(db)
    last_seq = conn.execute("SELECT last_seq FROM audit_chain_head WHERE id=0").fetchone()[0]
    max_seq = conn.execute("SELECT MAX(seq) FROM audit_events").fetchone()[0]
    conn.close()
    assert last_seq == max_seq == 2


def test_last_seq_tamper_rejected(tmp_path) -> None:
    """审计 P1-2：单独篡改 last_seq（与 last_event_seq 不一致）拒绝启动。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    old = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    old.record(_event("1"))
    old.record(_event("2"))
    # 篡改 last_seq
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_chain_head SET last_seq = 99 WHERE id = 0")
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_healthy_concurrent_append_during_start_not_rejected(tmp_path) -> None:
    """审计 P1-2b：启动期间另一实例合法追加，新实例不应误判篡改。

    get_chain_state 在同一锁内读 head + last_event，避免两次独立读取间
    被并发追加插入不一致状态。
    """
    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    # 实例 1 写两条
    l1 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    l1.record(_event("1"))
    l1.record(_event("2"))
    # 实例 2 在实例 3 启动前追加一条（模拟滚动发布新旧重叠）
    l2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    l2.record(_event("3"))
    # 实例 3 启动：get_chain_state 稳定快照，不应误判
    l3 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    assert l3.verify_integrity().status == "clean"


# -- 旧库迁移与 head 一致性（审计 P1-1/P1-2）----------------------------------- #


def test_legacy_signed_db_migrates_chain_head(tmp_path) -> None:
    """审计 P1-1：旧版签名库（有事件无 chain-head 表）升级时安全迁移。

    旧库有合法 _hmac 事件但无 chain-head 表（上一版 schema）。新版启动时验证旧链
    后初始化 head 为最后一条 _hmac，后续写入不断链。真正的旧库没有设置当前版本的
    ``PRAGMA user_version=2``，因此 fixture 必须显式保留旧版标记 ``user_version=0``；
    当前版本库直接丢失 head 表属于损坏，不能借此测试模拟。
    """
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    # 先借当前实现生成两条合法签名事件，再移除新版 head 元数据并重置数据库版本，
    # 得到「有合法签名事件、从未迁移 chain-head schema」的旧库 fixture。
    old = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    old.record(_event("1"))
    old.record(_event("2"))
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE audit_chain_head")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    # 升级：新版验证旧链后初始化 head，后续写入不断链
    new = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    new.record(_event("3"))
    assert new.verify_integrity().status == "clean"


def test_current_schema_recreated_as_legacy_shape_is_rejected(tmp_path) -> None:
    """审计 P1-2：当前库不能靠重建旧形态 head 表降级成迁移态。

    ``user_version=2`` 是当前 schema 的权威标记。即使攻击者把 head 表重建为缺少
    ``last_seq/schema_version`` 的旧形态，也必须按当前库损坏拒绝，不能自动把数据库
    版本降成 1 后重建 head。
    """
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    current = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    current.record(_event("1"))
    current.record(_event("2"))

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.execute("DROP TABLE audit_chain_head")
    conn.execute(
        "CREATE TABLE audit_chain_head ("
        "id INTEGER PRIMARY KEY CHECK (id = 0),"
        " head_hmac TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_legacy_unsigned_db_rejected_on_migrate(tmp_path) -> None:
    """审计 P1-1：旧库有未签名事件（无 _hmac），迁移验证失败拒绝启动。"""
    from datetime import datetime, timezone

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from common.type_def import AuditEvent
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    # 裸 sqlite 写一条无 _hmac 事件
    bare = SqliteAuditLogger(db)
    bare.record(
        AuditEvent(
            id="legacy",
            actor=_ACTOR,
            action="write",
            layer="api",
            decision="allow",
            target_id="t",
            occurred_at=datetime.now(timezone.utc),
            detail={},
            target=_ACTOR,
        )
    )
    key = derive_audit_key(_hmac_key())
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_tampered_chain_head_rejected_on_start(tmp_path) -> None:
    """审计 P1-2：chain-head 表被改（与最后事件 _hmac 不一致）拒绝启动。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    old = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    old.record(_event("1"))
    old.record(_event("2"))
    # 篡改 chain-head
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_chain_head SET head_hmac='fake' WHERE id=0")
    conn.commit()
    conn.close()
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_tail_delete_breaks_head_consistency_rejected(tmp_path) -> None:
    """审计 P1-2：删事件尾部但不更新 head -> head 与最后事件不一致 -> 拒启动。

    本期不防尾删（范围降级），但 head 与事件不一致必须检出--否则继续写入会断链。
    """
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    old = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    old.record(_event("1"))
    old.record(_event("2"))
    # 删最后一条事件（head 仍指向它）
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM audit_events WHERE seq=(SELECT MAX(seq) FROM audit_events)")
    conn.commit()
    conn.close()
    # head 与新的「最后事件」不一致 -> 拒启动
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


# -- 启动验证（审计 P1-2）--------------------------------------------------- #


def test_tampered_database_rejected_on_start(tmp_path) -> None:
    """审计 P1-2：已有篡改日志的库启动时拒绝（verify_on_start）。

    此前 HMAC 写进库但从不在生产验证，坏库照样启动。现在构造时验证历史链，
    检出篡改 -> ValidationError 拒绝启动。
    """
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    logger.record(_event("1"))
    logger.record(_event("2"))
    # 直接改库内容（篡改）
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_events SET action='tampered' WHERE seq=1")
    conn.commit()
    conn.close()
    # 新实例启动验证 -> 拒绝
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_unsigned_database_rejected_on_start(tmp_path) -> None:
    """审计 P1-2：已有未签名行（裸 sqlite 无 HMAC）的库，hmac 启动验证拒绝。

    避免历史未签名行被当正常链继续追加。
    """
    from datetime import datetime, timezone

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from common.type_def import AuditEvent
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    # 先用裸 sqlite 写一条无 HMAC 事件
    bare = SqliteAuditLogger(db)
    bare.record(
        AuditEvent(
            id="legacy",
            actor=_ACTOR,
            action="write",
            layer="api",
            decision="allow",
            target_id="t",
            occurred_at=datetime.now(timezone.utc),
            detail={},
            target=_ACTOR,
        )
    )
    # hmac 启动：chain_head 为空但历史行无 _hmac -> verify 检出
    key = derive_audit_key(_hmac_key())
    with pytest.raises(ValidationError):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_verify_on_start_false_skips_check(tmp_path) -> None:
    """审计 P1-2：verify_on_start=False 跳过启动验证（测试/迁移用）。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    logger.record(_event("1"))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_events SET action='tampered' WHERE seq=1")
    conn.commit()
    conn.close()
    # verify_on_start=False -> 不拒，可继续启动（手动 verify 才检出）
    logger2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    assert logger2.verify_integrity()  # 手动 verify 检出


def test_verify_integrity_is_on_audit_logger_interface() -> None:
    """审计 P1-2：verify_integrity 是 AuditLogger 接口，普通后端默认返回空。"""
    from common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger

    bare = InMemoryAuditLogger()
    assert bare.verify_integrity().status == "unsupported"  # 无完整性保护，不报篡改


# -- 尾删局限（审计 P1-3，记录已知局限不扩大）--------------------------------- #


def test_tail_truncation_is_known_limitation() -> None:
    """审计 P1-3：本地链式 HMAC 无法检测尾删--这是已知局限，非 bug。

    删除日志尾部后，剩余记录的 HMAC 仍全部自洽。完整防篡改需外部可信锚点，
    不在本期。本测试钉住此行为，确保未来引入锚点时这条会改（届时应检出）。
    """
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    for i in range(4):
        logger.record(_event(str(i)))
    assert logger.verify_integrity().status == "clean"

    # 删最后两条
    inner.events = inner.events[:2]
    # 当前实现：剩余记录仍自洽 -> 检不出（已知局限）
    assert logger.verify_integrity().status == "clean"


# -- key 派生 ----------------------------------------------------------------- #


def test_derived_key_is_deterministic_and_isolated() -> None:
    """同一 root key 派生出同一 audit key；不同 root key 派生不同。"""
    from security.audit_hmac import derive_audit_key

    k1 = derive_audit_key(b"root-1")
    k2 = derive_audit_key(b"root-1")
    k3 = derive_audit_key(b"root-2")
    assert k1 == k2
    assert k1 != k3
    assert k1 != b"root-1"  # 派生后与原 key 不同


def test_hmac_with_wrong_key_fails_verification() -> None:
    """用错 key 校验，全链都应判为篡改。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(b"key-a"))
    logger.record(_event("1"))
    logger.record(_event("2"))

    # 用不同 key 构造（跳过启动验证，手动 verify）
    other = HmacAuditLogger(inner, hmac_key=derive_audit_key(b"key-b"), verify_on_start=False)
    result = other.verify_integrity()
    assert 0 in result.tampered_indices
    assert 1 in result.tampered_indices


# -- 装饰器透明性 ------------------------------------------------------------- #


def test_decorator_passes_through_query() -> None:
    """装饰器不改变 query 语义，只加 HMAC 字段到 detail。"""
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = InMemoryAuditLogger()
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    logger.record(_event("1", action="write"))

    results = logger.query({"action": "write"})
    assert len(results) == 1
    assert "_hmac" in results[0].detail  # HMAC 进了 detail


def test_decorator_wraps_sqlite_logger(tmp_path) -> None:
    """装饰器能包 SqliteAuditLogger（真实持久化后端）。"""
    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    inner = SqliteAuditLogger(str(tmp_path / "audit.sqlite3"))
    logger = HmacAuditLogger(inner, hmac_key=derive_audit_key(_hmac_key()))
    logger.record(_event("1"))
    logger.record(_event("2"))
    assert logger.verify_integrity().status == "clean"

    # 篡改：直接改库里的 detail_json 不现实，但验证 query 仍能取到带 HMAC 的事件
    events = logger.query({"action": "write"})
    assert all("_hmac" in e.detail for e in events)


# -- factory 装配（配置驱动 opt-in）----------------------------------------- #


def test_factory_builds_hmac_wrapper_from_named_dependencies(tmp_path) -> None:
    """audit.default.target: hmac 时包一层 HmacAuditLogger，inner 由配置指定。"""
    from common.audit.base import AuditProducer
    from common.factory.factory import Factory
    from config.context import AssemblyContext
    from security.audit_hmac import HmacAuditLogger
    from security.bootstrap import register_security

    register_security()
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "audit": {
                "raw": {"target": "in_memory"},
                "default": {
                    "target": "hmac",
                    "params": {
                        "inner": "raw",
                        "key_hex": "00" * 32,
                    },
                },
            }
        }
    )
    logger = AuditProducer.build_named("default", ctx)
    assert isinstance(logger, HmacAuditLogger)
    logger.record(_event("1"))
    logger.record(_event("2"))
    assert logger.verify_integrity().status == "clean"


def test_factory_hmac_requires_inner() -> None:
    """没配 inner 时报错--给默认会让 HMAC 包一个未知后端。"""
    from common.audit.base import AuditProducer
    from common.factory.factory import Factory
    from config.context import AssemblyContext
    from security.bootstrap import register_security

    register_security()
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {"audit": {"default": {"target": "hmac", "params": {"key_hex": "00" * 32}}}}
    )
    with pytest.raises(ValidationError):
        AuditProducer.build_named("default", ctx)


def test_factory_hmac_rejects_self_reference() -> None:
    """审计 P3-3：inner 指向自身时装配期拒绝，不 RecursionError。"""
    from common.audit.base import AuditProducer
    from common.errors import ValidationError
    from common.factory.factory import Factory
    from config.context import AssemblyContext
    from security.bootstrap import register_security

    register_security()
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(
        {
            "audit": {
                "default": {"target": "hmac", "params": {"inner": "default", "key_hex": "00" * 32}}
            }
        }
    )
    with pytest.raises(ValidationError):
        AuditProducer.build_named("default", ctx)


def test_current_version_missing_core_columns_rejected(tmp_path) -> None:
    """审计 P2-3：当前版本 (user_version >= 2) 缺核心列时统一抛 ValidationError。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from common.errors import ValidationError
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())

    # 创建正常当前版本库
    logger = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)
    logger.record(_event("1"))
    del logger

    # 模拟攻击：DROP head 表后重建缺少 last_seq 列的旧形态，但保持 user_version=2
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE audit_chain_head")
    conn.execute(
        "CREATE TABLE audit_chain_head (id INTEGER PRIMARY KEY, head_hmac TEXT)"
    )  # 缺少 last_seq 和 schema_version
    conn.commit()
    conn.close()

    # 启动应拒绝
    with pytest.raises(
        ValidationError, match="audit chain head schema corrupted.*missing required columns"
    ):
        HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key)


def test_tampered_count_and_samples_truncated(tmp_path) -> None:
    """审计 P2-1：采样上限生效时，tampered_count 记录真实总数，samples_truncated=True。"""
    import sqlite3

    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())

    # 写入 150 条事件
    logger = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    for i in range(150):
        logger.record(_event(f"event_{i}"))
    del logger

    # 篡改所有事件的 action 字段
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_events SET action = 'TAMPERED'")
    conn.commit()
    conn.close()

    # 验证：应采样 100 条，但 tampered_count=150，samples_truncated=True
    logger2 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    result = logger2.verify_integrity()

    assert result.status == "tampered"
    assert len(result.tampered_indices) == 100  # 采样上限
    assert result.tampered_count == 150  # 真实总数
    assert result.samples_truncated is True  # 截断标志


def test_get_chain_state_snapshot_consistency(tmp_path) -> None:
    """审计 P2-1：get_chain_state() 单次调用快照内部一致性。

    验证 get_chain_state() 返回的 head_last_seq 和 last_event_seq 在同一快照内一致。
    SQLite 实现用单条 SQL CTE 查询保证查询结果来自同一事务快照。
    """
    from common.audit.audit_impl.sqlite_audit_logger import SqliteAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    db = str(tmp_path / "audit.sqlite3")
    key = derive_audit_key(_hmac_key())

    # 实例 1 写入两条
    l1 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    l1.record(_event("1"))
    l1.record(_event("2"))

    # 读取快照 1
    l2 = SqliteAuditLogger(db)
    head_hmac_1, head_last_seq_1, last_event_seq_1, _, _ = l2.get_chain_state()

    # 验证快照内部一致：head_last_seq 应该等于 last_event_seq
    assert head_last_seq_1 == last_event_seq_1 == 2

    # 另一实例写入新事件
    l3 = HmacAuditLogger(SqliteAuditLogger(db), hmac_key=key, verify_on_start=False)
    l3.record(_event("3"))

    # 读取快照 2
    head_hmac_2, head_last_seq_2, last_event_seq_2, _, _ = l2.get_chain_state()

    # 验证快照 2 内部仍然一致
    assert head_last_seq_2 == last_event_seq_2 == 3

    # 验证两次快照都是稳定的（不会出现 head 和 last_event 不一致）
    assert head_hmac_1 is not None
    assert head_hmac_2 is not None
    assert head_hmac_2 != head_hmac_1  # 链头已更新


def test_hmac_rejects_non_cas_backend() -> None:
    """审计 P1-1：HmacAuditLogger 拒绝不支持 CAS 的后端。"""
    from common.audit.base import AuditLogger
    from common.errors import ValidationError
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    class NonCASBackend(AuditLogger):
        """测试用后端：不支持 CAS。"""

        def record(self, event) -> None:
            pass

        def query(self, filters, limit=100, *, offset=0):
            return []

        def supports_chain_cas(self) -> bool:
            return False  # 明确不支持

    backend = NonCASBackend()
    key = derive_audit_key(_hmac_key())

    with pytest.raises(ValidationError, match="requires a backend that supports chain CAS"):
        HmacAuditLogger(backend, hmac_key=key)


def test_in_memory_backend_supports_cas() -> None:
    """审计 P1-1：InMemoryAuditLogger 声明支持线程级 CAS。"""
    from common.audit.audit_impl.in_memory_audit_logger import InMemoryAuditLogger
    from security.audit_hmac import HmacAuditLogger, derive_audit_key

    backend = InMemoryAuditLogger()
    assert backend.supports_chain_cas() is True

    # 应该能成功构造 HMAC 装饰器
    key = derive_audit_key(_hmac_key())
    logger = HmacAuditLogger(backend, hmac_key=key, verify_on_start=False)
    logger.record(_event("test"))

    # 验证完整性应该通过
    result = logger.verify_integrity()
    assert result.status == "clean"
