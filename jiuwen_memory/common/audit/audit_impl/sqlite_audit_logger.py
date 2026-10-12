# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SQLite-backed audit logger.

同时实现 :class:`~jiuwen_memory.common.audit.base.AuditLogger`（普通记录/查询）与
:class:`~jiuwen_memory.common.security.audit_integrity.chain_store.ChainedAuditStore`
（链式完整性）。完整性 schema 在首次以链式 capability 使用时建立
（``_ensure_integrity_schema``），严格区分空库 / 旧无签名库 / 合法现库 / 损坏库
（计划 §7.2）。

并发模型（计划 §1.3 经验 1、§12.2）：

- **实例级 RLock** 串行化同一连接的多线程；
- **SQLite ``BEGIN IMMEDIATE``** 串行化多连接 / 多实例对同一文件的写--CAS（比较链头、
  插入事件+proof、推进 head）放进同一 ``BEGIN IMMEDIATE`` 事务，跨进程也不分叉。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import NamedTuple

from jiuwen_memory.common.audit.base import AuditLogger, AuditProducer
from jiuwen_memory.common.security.audit_integrity.audit_integrity_impl.chained_hmac import (
    CANONICAL_FORMAT_VERSION,
)
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditMigrationRequiredError,
    AuditSchemaError,
    ChainConflictError,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
    Proof,
)
from jiuwen_memory.common.type_def import AuditEvent, Scope

# PRAGMA user_version 标记：0 = 普通审计库（无 proof），2 = 完整性 schema v1。
INTEGRITY_SCHEMA_VERSION = 2

_PROOF_COLUMNS = (
    "proof_format_version",
    "previous_digest",
    "digest",
    "key_id",
    "key_epoch",
)

# 完整性库必须齐备的核心事件列（PR3-03：严格校验覆盖全部核心列，不只见 proof 列）。
_CORE_COLUMNS = (
    "seq",
    "id",
    "actor_org",
    "actor_space",
    "actor_user",
    "actor_agent",
    "actor_session",
    "target_org",
    "target_space",
    "target_user",
    "target_agent",
    "target_session",
    "action",
    "target_id",
    "layer",
    "decision",
    "occurred_at",
    "detail_json",
)

_SQLITE_CAP = ChainStoreCapability(
    persistent=True,
    atomic_append=True,
    stable_head_snapshot=True,
    key_epoch=True,
    external_anchor=False,
    streaming_scan=True,
)

# scan 的单页硬上限：防止恶意大 limit 造成无界读取（服务端 limits 之外的第二道边界）。
_SCAN_HARD_LIMIT = 10_000


class AuditRow(NamedTuple):
    id: str
    actor_org: str
    actor_space: str
    actor_user: str
    actor_agent: str
    actor_session: str
    target_org: str
    target_space: str
    target_user: str
    target_agent: str
    target_session: str
    action: str
    target_id: str
    layer: str
    decision: str
    occurred_at: str | None
    detail_json: str


class SqliteAuditLogger(AuditLogger, ChainedAuditStore):
    """Persist audit events in a local SQLite database, with optional chain integrity."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = RLock()
        # capability 按真实存储形态声明（PR3-04/R6）：``:memory:`` 与空路径都是 SQLite
        # 临时数据库（连接关闭即丢链），必须标注非持久，才能被「非持久链禁止生产」的
        # 装配判断拒绝；不能因为类是 SQLite 就声明 persistent。
        temporary = db_path == ":memory:" or not db_path
        if not temporary:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._cap = (
            _SQLITE_CAP
            if not temporary
            else ChainStoreCapability(
                persistent=False,
                atomic_append=True,
                stable_head_snapshot=True,
                key_epoch=True,
                external_anchor=False,
                streaming_scan=True,
            )
        )
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # BEGIN IMMEDIATE 在另一连接持锁时默认立即抛 "database is locked"；给一个 busy
        # timeout 让它等待，减少跨连接竞争下的伪 CAS 冲突，让真正的「head 已推进」冲突
        # 成为重试触发条件（计划 §12.2）。
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()
        self._integrity_ready = False

    def record(self, event: AuditEvent) -> None:
        self._record_many([event])

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        with self._lock:
            clauses: list[str] = []
            params: list[object] = []
            exact_fields = {
                "action": "action",
                "layer": "layer",
                "decision": "decision",
                "target_id": "target_id",
                "actor_org": "actor_org",
                "actor_space": "actor_space",
                "actor_user": "actor_user",
                "actor_agent": "actor_agent",
                "actor_session": "actor_session",
                "target_org": "target_org",
                "target_space": "target_space",
                "target_user": "target_user",
                "target_agent": "target_agent",
                "target_session": "target_session",
            }
            for field, column in exact_fields.items():
                if filters.get(field):
                    clauses.append(f"{column} = ?")
                    params.append(filters[field])
            if filters.get("occurred_after"):
                clauses.append("datetime(occurred_at) >= datetime(?)")
                params.append(filters["occurred_after"])
            if filters.get("occurred_before"):
                clauses.append("datetime(occurred_at) <= datetime(?)")
                params.append(filters["occurred_before"])
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = self._conn.execute(
                f"SELECT * FROM audit_events {where} ORDER BY seq ASC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def _init_schema(self) -> None:
        """普通审计 schema（无 proof 列）。完整性 schema 由 ``_ensure_integrity_schema`` 建立。

        已标记完整性版本（user_version>=2）的库禁止任何 DDL 修补（PR3-03）：缺列、
        缺表属于损坏，由 ``_ensure_integrity_schema`` / ``health()`` 严格拒绝，不在
        初始化时静默补列放行。
        """
        with self._lock, self._conn:
            user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if user_version >= INTEGRITY_SCHEMA_VERSION:
                return
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL,
                    actor_org TEXT NOT NULL,
                    actor_space TEXT NOT NULL,
                    actor_user TEXT NOT NULL,
                    actor_agent TEXT NOT NULL,
                    actor_session TEXT NOT NULL,
                    target_org TEXT NOT NULL,
                    target_space TEXT NOT NULL,
                    target_user TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    target_session TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    occurred_at TEXT,
                    detail_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_events_action_layer "
                "ON audit_events(action, layer, seq)"
            )
            column_rows = self._conn.execute("PRAGMA table_info(audit_events)").fetchall()
            columns = {row["name"] for row in column_rows}
            if "actor_space" not in columns:
                self._conn.execute(
                    "ALTER TABLE audit_events ADD COLUMN actor_space TEXT NOT NULL DEFAULT ''"
                )
            for column in (
                "target_org",
                "target_space",
                "target_user",
                "target_agent",
                "target_session",
            ):
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE audit_events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )

    def _record_many(self, events: Iterable[AuditEvent]) -> None:
        """Internal bulk insert hook for a future public record_many API."""
        rows = [_event_to_row(event) for event in events]
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO audit_events (
                    id, actor_org, actor_space, actor_user, actor_agent, actor_session,
                    target_org, target_space, target_user, target_agent, target_session,
                    action, target_id, layer, decision, occurred_at, detail_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    # -- ChainedAuditStore 契约 ------------------------------------------- #

    def capabilities(self) -> ChainStoreCapability:
        return self._cap

    def health(self) -> None:
        # 全程持实例锁（R2）：health 的 BEGIN/COMMIT 与 append/read_stable_snapshot
        # 共享同一连接，无锁并发会在另一线程的事务里嵌套 BEGIN，被误当 CAS 冲突耗尽
        # 重试。RLock 可重入，_ensure_integrity_schema 内部的持锁调用不受影响。
        with self._lock:
            self._ensure_integrity_schema()
            # 运行期不能被初始化缓存放行（PR3-01）：每次真实核对 head 与末事件一致性。
            # 初始化结果可缓存，schema 版本核验不可（R5）：初始化后 user_version 被改
            # （升到未知版本或降级）都是运行期损坏，必须拒绝。
            user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if user_version != INTEGRITY_SCHEMA_VERSION:
                raise AuditSchemaError(
                    f"audit database user_version changed to {user_version} at runtime; "
                    f"expected {INTEGRITY_SCHEMA_VERSION} (integrity schema damaged)"
                )
            self._verify_integrity_consistency()

    def read_head(self) -> ChainHead:
        with self._lock:
            self._ensure_integrity_schema()
            return self._read_head_locked()

    def append(self, record: ChainedRecord, expected_head: ChainHead) -> ChainHead:
        """原子 CAS 追加：比较链头、插入事件+proof、推进 head，同一事务（计划 §4.3）。"""
        with self._lock:
            self._ensure_integrity_schema()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # 另一连接持有写锁：当作 CAS 冲突，由协调器重试。
                raise ChainConflictError("sqlite write lock busy") from exc
            try:
                current = self._read_head_locked()
                if (
                    current.sequence != expected_head.sequence
                    or current.digest != expected_head.digest
                ):
                    raise ChainConflictError("sqlite chain head moved")
                proof = record.proof
                # sequence 由链头位置决定（=expected_head.sequence+1），不由
                # AUTOINCREMENT 分配；避免 CAS 重试产生 seq 间隙导致 proof.sequence
                # 与后端分配值不一致。CAS 由 BEGIN IMMEDIATE + head 比较保证：持有
                # 写锁期间他连接无法推进 head，故 head.sequence+1 必为下一个空位。
                new_seq = expected_head.sequence + 1
                if new_seq != proof.sequence:
                    raise AuditSchemaError(
                        f"proof.sequence {proof.sequence} != expected head+1 {new_seq}"
                    )
                row = _event_to_row(record.event)
                self._conn.execute(
                    """
                    INSERT INTO audit_events (
                        seq,
                        id, actor_org, actor_space, actor_user, actor_agent, actor_session,
                        target_org, target_space, target_user, target_agent, target_session,
                        action, target_id, layer, decision, occurred_at, detail_json,
                        proof_format_version, previous_digest, digest, key_id, key_epoch
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_seq,
                        row.id,
                        row.actor_org,
                        row.actor_space,
                        row.actor_user,
                        row.actor_agent,
                        row.actor_session,
                        row.target_org,
                        row.target_space,
                        row.target_user,
                        row.target_agent,
                        row.target_session,
                        row.action,
                        row.target_id,
                        row.layer,
                        row.decision,
                        row.occurred_at,
                        row.detail_json,
                        proof.format_version,
                        proof.previous_digest,
                        proof.digest,
                        proof.key_id,
                        proof.key_epoch,
                    ),
                )
                new_head = ChainHead(
                    sequence=new_seq,
                    digest=proof.digest,
                    key_id=proof.key_id,
                    key_epoch=proof.key_epoch,
                    format_version=proof.format_version,
                )
                self._conn.execute(
                    """
                    UPDATE audit_chain_head
                    SET sequence = ?, digest = ?, key_id = ?, key_epoch = ?, format_version = ?
                    WHERE id = 1
                    """,
                    (
                        new_head.sequence,
                        new_head.digest,
                        new_head.key_id,
                        new_head.key_epoch,
                        new_head.format_version,
                    ),
                )
                self._conn.execute("COMMIT")
            except ChainConflictError:
                self._conn.execute("ROLLBACK")
                raise
            except AuditSchemaError:
                self._conn.execute("ROLLBACK")
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return new_head

    def read_stable_snapshot(self, after_sequence: int = 0) -> ChainSnapshot:
        with self._lock:
            self._ensure_integrity_schema()
            # 单事务内一致读取 head、真实末事件与 checkpoint（计划 §1.3 经验 3），并在
            # 同一快照内核对 head 与真实末行 / genesis 条件（PR3-01：不信任未经核对的
            # head 来缩小验证窗口）。合法并发写入只能追加，故此核对不影响固定快照语义。
            self._conn.execute("BEGIN")
            try:
                head = self._read_head_locked()
                last_row = self._conn.execute(
                    "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                checkpoint_row = (
                    self._conn.execute(
                        "SELECT * FROM audit_events WHERE seq = ? ",
                        (after_sequence,),
                    ).fetchone()
                    if after_sequence > 0
                    else None
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._check_head_against_last(head, last_row)
            last_record = _row_to_chained(last_row) if last_row else None
            checkpoint = _row_to_chained(checkpoint_row) if checkpoint_row else None
            return ChainSnapshot(
                head=head,
                last_record=last_record,
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
        """keyset 扫描：``WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT``（计划 §12.5）。

        上界固定为调用方传入的快照 ``head.sequence``（不用 OFFSET，不重读当前 head）。
        """
        if limit < 1:
            return []
        # 上界约束，防止恶意大 limit 造成无界读取。
        limit = min(limit, _SCAN_HARD_LIMIT)
        with self._lock:
            self._ensure_integrity_schema()
            rows = self._conn.execute(
                "SELECT * FROM audit_events WHERE seq > ? AND seq <= ? ORDER BY seq ASC LIMIT ?",
                (after_sequence, through_sequence, limit),
            ).fetchall()
        return [_row_to_chained(row) for row in rows]

    # -- 完整性 schema 建立 / 严格校验（计划 §7.2） ------------------------ #

    def _ensure_integrity_schema(self) -> None:
        """幂等地建立完整性 schema 并严格校验（计划 §7.2）。

        - 空库（无事件、user_version<2）：建立 proof 列 + head 行 + genesis，置 user_version=2；
        - 旧无签名库（有事件、user_version<2）：抛 ``AuditMigrationRequiredError``，不静默启动；
        - 合法现库（user_version=2）：严格校验全部核心列/表、head 行、head 与末事件一致，
          损坏抛 ``AuditSchemaError``，不做任何 DDL 修补（PR3-03）；
        - 未知未来版本（user_version>2）：拒绝，不当作当前版本放行（PR3-03）；
        - 缺表/缺列/缺 head 行：不自动重建放行（计划 §7.2.5）。
        """
        if self._integrity_ready:
            return
        with self._lock:
            user_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if user_version > INTEGRITY_SCHEMA_VERSION:
                raise AuditSchemaError(
                    f"audit database user_version {user_version} is newer than the "
                    f"integrity schema version {INTEGRITY_SCHEMA_VERSION} this build "
                    "understands; refusing to open as integrity-enabled"
                )
            column_rows = self._conn.execute("PRAGMA table_info(audit_events)").fetchall()
            columns = {row["name"] for row in column_rows}
            row_count = self._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

            if user_version < INTEGRITY_SCHEMA_VERSION:
                if row_count > 0:
                    # 有历史事件但无 proof：拒绝启动，不补签（计划 §7.2.3）。
                    raise AuditMigrationRequiredError(
                        "audit database has unsigned historical events and cannot be "
                        "auto-migrated; back up, clear, or migrate offline before enabling "
                        "audit integrity"
                    )
                # 空库：建立完整性 schema 与 genesis head。
                self._add_proof_columns(columns)
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_chain_head (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        sequence INTEGER NOT NULL,
                        digest TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        key_epoch INTEGER NOT NULL,
                        format_version INTEGER NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO audit_chain_head
                        (id, sequence, digest, key_id, key_epoch, format_version)
                    VALUES (1, 0, ?, '', 0, ?)
                    """,
                    (GENESIS_DIGEST, CANONICAL_FORMAT_VERSION),
                )
                self._conn.execute(f"PRAGMA user_version = {INTEGRITY_SCHEMA_VERSION}")
                self._conn.commit()
            else:
                # user_version == 2：严格校验，禁止 DDL 修补（PR3-03）。
                missing = (set(_CORE_COLUMNS) | set(_PROOF_COLUMNS)) - columns
                if missing:
                    raise AuditSchemaError(
                        "audit integrity schema missing columns: " + ", ".join(sorted(missing))
                    )
                table_rows = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                tables = {row[0] for row in table_rows}
                if "audit_chain_head" not in tables:
                    raise AuditSchemaError("audit_chain_head table missing (user_version=2)")
                self._verify_integrity_consistency()
            self._integrity_ready = True

    def _add_proof_columns(self, existing_columns: set[str]) -> None:
        """为 audit_events 增加可空 proof 列（空库升级时调用）。"""
        if "proof_format_version" not in existing_columns:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN proof_format_version INTEGER")
        if "previous_digest" not in existing_columns:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN previous_digest TEXT")
        if "digest" not in existing_columns:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN digest TEXT")
        if "key_id" not in existing_columns:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN key_id TEXT")
        if "key_epoch" not in existing_columns:
            self._conn.execute("ALTER TABLE audit_events ADD COLUMN key_epoch INTEGER")

    def _verify_integrity_consistency(self) -> None:
        """校验 head 行存在且与真实末事件一致（计划 §1.3 经验 3、§7.2.5；PR3-01）。

        head 与末事件必须在同一事务快照内读取，否则跨连接并发提交期间会看到
        head（旧）与末事件（新）的不一致状态而误报 ``AuditSchemaError``。
        """
        self._conn.execute("BEGIN")
        try:
            head_row = self._conn.execute(
                "SELECT sequence, digest, key_id, key_epoch, format_version "
                "FROM audit_chain_head WHERE id = 1"
            ).fetchone()
            last_row = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        if head_row is None:
            raise AuditSchemaError("audit_chain_head row missing")
        self._check_head_against_last(
            ChainHead(
                sequence=head_row["sequence"],
                digest=head_row["digest"],
                key_id=head_row["key_id"],
                key_epoch=head_row["key_epoch"],
                format_version=head_row["format_version"],
            ),
            last_row,
        )

    def _check_head_against_last(self, head: ChainHead, last_row) -> None:
        """同事务快照内核对 head 与真实末事件 / genesis 固定值（PR3-01/R4）。

        非空链必须逐字段一致（sequence/digest/key_id/key_epoch/format_version），
        不能只比摘要——head 的 key ref 与末 proof 的 key ref 不一致意味着链头被
        伪造成另一把密钥签的样子。genesis（sequence=0）核对固定值：无事件、digest
        为 :data:`GENESIS_DIGEST`、空 key ref、当前格式版本。
        """
        if head.sequence == 0:
            if last_row is not None:
                raise AuditSchemaError("chain head at genesis but audit_events non-empty")
            if head.digest != GENESIS_DIGEST:
                raise AuditSchemaError("genesis chain head digest != GENESIS_DIGEST")
            if head.key_id != "" or head.key_epoch != 0:
                raise AuditSchemaError("genesis chain head must carry an empty key ref")
            if head.format_version != CANONICAL_FORMAT_VERSION:
                raise AuditSchemaError(
                    f"chain head format_version {head.format_version} unsupported"
                )
            return
        if last_row is None:
            raise AuditSchemaError("chain head beyond genesis but audit_events empty")
        if head.sequence != last_row["seq"]:
            raise AuditSchemaError(
                f"chain head sequence {head.sequence} != last event seq {last_row['seq']}"
            )
        if head.digest != last_row["digest"]:
            raise AuditSchemaError("chain head digest != last event proof digest")
        if head.key_id != last_row["key_id"]:
            raise AuditSchemaError("chain head key_id != last event proof key_id")
        if head.key_epoch != last_row["key_epoch"]:
            raise AuditSchemaError("chain head key_epoch != last event proof key_epoch")
        if head.format_version != CANONICAL_FORMAT_VERSION:
            raise AuditSchemaError(f"chain head format_version {head.format_version} unsupported")

    def _read_head_locked(self) -> ChainHead:
        row = self._conn.execute(
            "SELECT sequence, digest, key_id, key_epoch, format_version "
            "FROM audit_chain_head WHERE id = 1"
        ).fetchone()
        if row is None:
            # _ensure_integrity_schema 已保证 head 行存在；到这说明状态损坏。
            raise AuditSchemaError("audit_chain_head row missing after integrity init")
        return ChainHead(
            sequence=row["sequence"],
            digest=row["digest"],
            key_id=row["key_id"],
            key_epoch=row["key_epoch"],
            format_version=row["format_version"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@AuditProducer.register("sqlite")
def _build(config):
    return SqliteAuditLogger(config.get("db_path", ":memory:"))


def _event_to_row(event: AuditEvent) -> AuditRow:
    occurred_at = event.occurred_at.isoformat() if event.occurred_at else None
    detail_json = json.dumps(event.detail, ensure_ascii=False, sort_keys=True)
    return AuditRow(
        id=event.id,
        actor_org=event.actor.org,
        actor_space=event.actor.space,
        actor_user=event.actor.user,
        actor_agent=event.actor.agent,
        actor_session=event.actor.session,
        target_org=event.target.org,
        target_space=event.target.space,
        target_user=event.target.user,
        target_agent=event.target.agent,
        target_session=event.target.session,
        action=event.action,
        target_id=event.target_id,
        layer=event.layer,
        decision=event.decision,
        occurred_at=occurred_at,
        detail_json=detail_json,
    )


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    occurred_at = row["occurred_at"]
    return AuditEvent(
        id=row["id"],
        actor=Scope(
            org=row["actor_org"],
            space=row["actor_space"],
            user=row["actor_user"],
            agent=row["actor_agent"],
            session=row["actor_session"],
        ),
        target=Scope(
            org=row["target_org"],
            space=row["target_space"],
            user=row["target_user"],
            agent=row["target_agent"],
            session=row["target_session"],
        ),
        action=row["action"],
        target_id=row["target_id"],
        layer=row["layer"],
        decision=row["decision"],
        occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
        detail=json.loads(row["detail_json"]),
    )


def _row_to_chained(row: sqlite3.Row) -> ChainedRecord:
    """从带 proof 列的行重建 ChainedRecord（任何 proof 列缺失/非法不修补，PR3-02）。

    NULL-proof 行（普通记录混入完整性库、或 proof 被剥离/篡改成非法值）无法用合法
    Proof 表达——契约要求 format_version/key_epoch 为正整数。这里不以默认值修补证据
    （NULL epoch 补 1 会让 HMAC 校验用修补后的材料），而以空 digest / 空 key_id 作
    sentinel：provider 对这类记录报 incomplete（证据不足），不当 clean。
    """
    event = _row_to_event(row)
    format_version = row["proof_format_version"]
    previous_digest = row["previous_digest"]
    digest = row["digest"]
    key_id = row["key_id"]
    key_epoch = row["key_epoch"]
    has_digests = digest is not None and previous_digest is not None and key_id is not None
    valid_format = (
        isinstance(format_version, int)
        and not isinstance(format_version, bool)
        and format_version >= 1
    )
    valid_epoch = isinstance(key_epoch, int) and not isinstance(key_epoch, bool) and key_epoch >= 1
    proof_intact = has_digests and valid_format and valid_epoch
    if proof_intact:
        proof = Proof(
            format_version=format_version,
            sequence=row["seq"],
            previous_digest=previous_digest,
            digest=digest,
            key_id=key_id,
            key_epoch=key_epoch,
        )
    else:
        proof = Proof(
            format_version=CANONICAL_FORMAT_VERSION,
            sequence=row["seq"],
            previous_digest="",
            digest="",
            key_id="",
            key_epoch=1,
        )
    return ChainedRecord(event=event, proof=proof)
