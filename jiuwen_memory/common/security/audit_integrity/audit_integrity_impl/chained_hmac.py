# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""版本化规范化 + 链式 HMAC 审计完整性实现（F05 §Audit Integrity、计划 §4.4、§4.5）。

设计要点：

- **规范化有独立版本**（``CANONICAL_FORMAT_VERSION``），覆盖 AuditEvent 全部受保护字段、
  actor/target Scope 五维、``occurred_at`` 的 UTC 规范表示、detail 的稳定 key 顺序与
  UTF-8 编码，以及 sequence / previous digest / key id / epoch / format version（计划
  §4.5）。禁止依赖 dataclass ``repr``、Python dict 插入顺序、默认时区或数据库 JSON
  输出。
- **digest 比较走常时间 API**（``hmac.compare_digest``）；错误消息不含秘密（计划 §12.1）。
- **未知格式版本返回 incomplete 并拒绝当 clean**（计划 §4.5）。
- **签发用活动 key，验证按 proof 自带 KeyRef 选历史材料**，不回退活动 key 试验（计划
  §5.2）。
- **CAS 冲突有界重试**，超限抛
  :class:`~jiuwen_memory.common.security.audit_integrity.base.ChainConflictError`
  （计划 §4.3）。
- **增量验证走 checkpoint**（F05 §6.1）：``after_sequence > 0`` 时从稳定快照取第
  ``after_sequence`` 条记录，先验证其 proof（计入 ``checked_count``，通过即把
  ``high_water_mark`` 推进到 ``after_sequence``），再以它的 digest 为续链基线分页扫描。
  checkpoint 不存在返回 incomplete，不回落 genesis、不跳到下一条。分页扫描固定传快照的
  ``head.sequence`` 作上界，页空、序号缺口或未达链头一律 incomplete，只有恰好到达
  链头才可报告 clean。
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from datetime import UTC, datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.audit_integrity.base import (
    DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    AnchorState,
    AnchorStatus,
    AuditIntegrityProducer,
    AuditIntegrityProvider,
    AuditIntegrityStatus,
    AuditSchemaError,
    AuditVerificationResult,
    ChainConflictError,
    KeyCapabilityError,
    Proof,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
)
from jiuwen_memory.common.security.cryptography.base import KeyMismatchError
from jiuwen_memory.common.security.cryptography.key_provider import KeyProvider, KeyRef
from jiuwen_memory.common.type_def import AuditEvent, Scope

_LOG = logging.getLogger(__name__)

CANONICAL_FORMAT_VERSION = 1
"""规范化与证明格式版本（计划 §4.5）。未知版本返回 incomplete，拒绝当 clean。"""

AUDIT_MAC_PURPOSE = "audit-integrity:hmac:v1"
"""审计 MAC 的固定用途标签（计划 §5.1）。与加密包裹密钥派生互不复用（F05 §密钥隔离）。"""

DEFAULT_MAX_CAS_RETRIES = 10
DEFAULT_MAX_KEY_RETRIES = 5


# ====================================================================== #
# 版本化规范化（计划 §4.5）
# ====================================================================== #


def canonical_event_bytes(
    event: AuditEvent,
    *,
    sequence: int,
    previous_digest: str,
    key_ref: KeyRef,
) -> bytes:
    """把 AuditEvent + 链上下文规范化为稳定字节（计划 §4.5）。

    稳定性来源：``sort_keys=True`` 固定 key 顺序、``separators=(",",":")`` 消除空白、
    ``ensure_ascii=False`` + UTF-8 保留 Unicode；``occurred_at`` 统一转 UTC ISO。不依赖
    dataclass ``repr``、dict 插入顺序或数据库 JSON 输出。
    """
    payload = {
        "format_version": CANONICAL_FORMAT_VERSION,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "key_id": key_ref.key_id,
        "key_epoch": key_ref.epoch,
        "event": {
            "id": event.id,
            "actor": _scope_payload(event.actor),
            "target": _scope_payload(event.target),
            "action": event.action,
            "target_id": event.target_id,
            "layer": event.layer,
            "decision": event.decision,
            "occurred_at": _canonical_ts(event.occurred_at),
            "detail": {str(k): str(v) for k, v in event.detail.items()},
        },
    }
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return material.encode("utf-8")


def _scope_payload(scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": scope.space,
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _canonical_ts(value: datetime | None) -> str:
    """``occurred_at`` 的 UTC 规范表示（计划 §4.5）。

    naive 视为 UTC（不调 ``astimezone`` -- 它会按进程本地时区解释 naive，引入隐藏状态）；
    aware 统一转 UTC。``None`` 记为空串。规范化是纯函数，签发与验证对同一 instant 产出
    相同字节。
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat()


# ====================================================================== #
# Provider
# ====================================================================== #


class ChainedHmacAuditIntegrityProvider(AuditIntegrityProvider):
    """版本化规范化 + HMAC-SHA256 链式证明 + 有界 CAS 协调（计划 §4.4、§4.5）。

    Provider 协调 :class:`ChainedAuditStore` 的原子追加与有界冲突重试；密钥经
    :class:`KeyProvider` 的 MAC capability 取得，不读取 root key bytes（计划 §5.1）。
    装配期校验后端 capability 与 key MAC capability，任一不足即拒绝（fail-closed）。
    """

    # 保留现有 provider 装配参数，避免门禁整理改变调用接口。
    def __init__(  # pylint: disable=too-many-arguments
        self,
        store: ChainedAuditStore,
        key_provider: KeyProvider,
        *,
        max_cas_retries: int = DEFAULT_MAX_CAS_RETRIES,
        max_key_retries: int = DEFAULT_MAX_KEY_RETRIES,
        verify_page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
        max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
        anchor=None,
        chain_id: str = "default",
    ) -> None:
        if max_cas_retries < 1:
            raise ValidationError(f"max_cas_retries 须 >= 1，得到 {max_cas_retries}")
        if max_key_retries < 1:
            raise ValidationError(f"max_key_retries 须 >= 1，得到 {max_key_retries}")
        if verify_page_size < 1:
            raise ValidationError(f"verify_page_size 须 >= 1，得到 {verify_page_size}")
        if max_samples < 0:
            raise ValidationError(f"max_samples 须 >= 0，得到 {max_samples}")
        if not isinstance(store, ChainedAuditStore):
            raise ValidationError("audit_integrity store 必须实现 ChainedAuditStore")
        if not isinstance(key_provider, KeyProvider):
            raise ValidationError("audit_integrity key_provider 必须是 KeyProvider 实现")
        if not key_provider.supports_mac():
            # capability 不足即拒绝，不靠 target 名判断（F05 §依据 capability 做安全决策）。
            raise KeyCapabilityError(
                "audit_integrity 的 key_provider 不提供 MAC capability；"
                "需要支持 MAC 的 KeyProvider（如 local）"
            )
        self._validate_store_capabilities(store.capabilities())
        self._store = store
        self._keys = key_provider
        self._max_cas_retries = max_cas_retries
        self._max_key_retries = max_key_retries
        self._verify_page_size = verify_page_size
        self._max_samples = max_samples
        self._anchor = anchor
        self._chain_id = chain_id

    # -- 装配期 capability 校验 ------------------------------------------- #

    @staticmethod
    def _validate_store_capabilities(cap: ChainStoreCapability) -> None:
        """完整性保护所需的硬性 capability（计划 §4.3、§8.2 不变量 5）。

        ``persistent`` 与 ``external_anchor`` 不强制：进程内临时链可用于 dev/测试（声明
        ``is_test_only``），外部锚点是可选能力。但原子追加、稳定快照、key epoch、流式
        扫描是完整性保证的必要条件，缺一即拒。
        """
        missing = []
        if not cap.atomic_append:
            missing.append("atomic_append")
        if not cap.stable_head_snapshot:
            missing.append("stable_head_snapshot")
        if not cap.key_epoch:
            missing.append("key_epoch")
        if not cap.streaming_scan:
            missing.append("streaming_scan")
        if missing:
            raise ValidationError("audit_integrity 后端缺少必需 capability: " + ", ".join(missing))

    # -- AuditIntegrityProvider 契约 -------------------------------------- #

    def capabilities(self) -> ChainStoreCapability:
        return self._store.capabilities()

    def chain_store(self) -> ChainedAuditStore:
        """provider 实际写入和验证的 store 实例（装配层据对象 identity 校验）。"""
        return self._store

    def active_key_ref(self) -> KeyRef:
        return self._keys.active_key()

    def is_test_only(self) -> bool:
        # 进程内非持久后端只能用于 dev/测试（计划 §8.3）。
        return not self._store.capabilities().persistent

    def health(self) -> None:
        self._store.health()
        self._keys.health()

    def record_chained(self, event: AuditEvent) -> ChainedRecord:
        """规范化、计算 proof、原子追加（有界 CAS 重试，计划 §4.3、§4.4）。

        轮换边界判定基于签名后的实际 proof 而非签名前的 ``active_key()``——签名期间
        ``mac()`` 可能返回与 ``active_key()`` 不同的 KeyRef，预判必然有竞态窗口。
        先签发业务事件，若 proof 的 key epoch/id 与链头不同则插入轮换边界再重签。

        轮换边界成功插入不属于 CAS 冲突（Q2）：只有 ``_store.append`` 抛
        ``ChainConflictError`` 才算消耗重试预算，合法配置 ``max_cas_retries=1``
        也能完成一次正常写入含轮换边界。
        """
        last_conflict: ChainConflictError | None = None
        cas_conflicts = 0
        # U1：成功轮换插入也有独立预算——持续轮换导致外层无限循环时在这里截断。
        # _append_rotation_event 自身的局部 retry 不能限制整个 record_chained 的总工作量。
        rotations_inserted = 0
        while cas_conflicts < self._max_cas_retries:
            head = self._store.read_head()
            sequence = head.sequence + 1
            previous_digest = head.digest if head.sequence > 0 else GENESIS_DIGEST
            proof = self._sign(event, sequence=sequence, previous_digest=previous_digest)

            # 签发后检测密钥是否已前进（T2 修复）：proof 的 key ref 来自 _sign 内
            # mac() 返回的实际密钥，比外部 active_key() 的瞬时快照可靠。
            if head.sequence > 0 and (
                head.key_id != proof.key_id or head.key_epoch != proof.key_epoch
            ):
                if rotations_inserted >= self._max_key_retries:
                    raise KeyCapabilityError(
                        "audit key rotated repeatedly during a single record_chained "
                        f"call; rotation budget exhausted after {self._max_key_retries} "
                        "insertion(s)"
                    )
                try:
                    head = self._append_rotation_event(head)
                except ChainConflictError as exc:
                    # 轮换事件 CAS 冲突：真正的并发写入者抢先推进链头。
                    last_conflict = exc
                    cas_conflicts += 1
                    continue
                # 轮换事件已成功推进链头，不计入 CAS 冲突（Q2）。
                # 但每次成功插入消耗一次轮换预算（U1）。
                rotations_inserted += 1
                # 重新签发业务事件以对齐新的 previous_digest 与 sequence。
                continue

            record = ChainedRecord(event=event, proof=proof)
            try:
                self._store.append(record, expected_head=head)
            except ChainConflictError as exc:
                # CAS 冲突：另一 writer 抢先推进了链头。重读 head 并有限重试，不无限自旋。
                last_conflict = exc
                cas_conflicts += 1
                continue
            return record
        raise ChainConflictError(
            "audit chain append CAS retry exhausted; "
            f"concurrent writers exceeded {self._max_cas_retries} attempts"
        ) from last_conflict

    def _append_rotation_event(self, head: ChainHead) -> ChainHead:
        """把一次密钥轮换边界写进链（计划 §5.2），返回推进后的新链头。

        用 ``active_key()`` 构造事件并签发；若签发期间轮换（proof 的 key epoch 与
        ``active_key()`` 不同），本方法在有界循环内用 proof 的实际 epoch 重建 detail
        并重签，直到 detail 与 proof 的 KeyRef 一致（Q1：不以固定次数修补跨越多次轮换
        的竞态）。
        """
        for _ in range(self._max_key_retries):
            active = self._keys.active_key()
            event = AuditEvent(
                id=uuid.uuid4().hex,
                actor=Scope(),
                action="key_rotate",
                layer="security",
                decision="allow",
                occurred_at=datetime.now(UTC),
                detail={
                    "key_id": active.key_id,
                    "key_epoch": str(active.epoch),
                    "previous_key_epoch": str(head.key_epoch),
                },
            )
            proof = self._sign(event, sequence=head.sequence + 1, previous_digest=head.digest)
            if proof.key_epoch == active.epoch and proof.key_id == active.key_id:
                # detail 与实际使用的密钥一致；存储的 canonical 字节与证明自洽。
                return self._store.append(
                    ChainedRecord(event=event, proof=proof), expected_head=head
                )
            # 签发期间密钥再次轮换：detail 已过期。proof 的 KeyRef 是权威值——
            # 下轮迭代用 active_key() 重建事件（它现在可能已推进到新 epoch）。
        raise KeyCapabilityError(
            "audit key changed repeatedly during rotation event signing; "
            f"detail could not be aligned with proof after {self._max_key_retries} attempts"
        )

    def verify(
        self,
        *,
        after_sequence: int = 0,
        page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
        max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
        anchor_policy: str = "if_configured",
    ) -> AuditVerificationResult:
        """流式验证稳定快照（计划 §9.1、§9.2、§12.1；F05 §6.1 增量语义）。

        keyset 分页扫描固定在快照 ``head.sequence``，内存随 page size / 样本上限有界，
        不随总日志量线性增长（计划 §12.5）。验证按 proof 自带 KeyRef 选历史材料；key
        不可用归 incomplete，tag 不匹配归 tampered，未知格式归 incomplete--均不当 clean。
        """
        if after_sequence < 0:
            raise ValidationError(f"after_sequence 须 >= 0，得到 {after_sequence}")
        if page_size < 1:
            raise ValidationError(f"page_size 须 >= 1，得到 {page_size}")
        if max_samples < 0:
            raise ValidationError(f"max_samples 须 >= 0，得到 {max_samples}")
        if anchor_policy not in {"if_configured", "required", "skip"}:
            raise ValidationError(
                f"anchor_policy 须为 if_configured/required/skip，得到 {anchor_policy!r}"
            )
        page = min(page_size, self._verify_page_size) if page_size else self._verify_page_size
        # 与 page_size 同语义：请求值与服务端配置上限取小者，构造配置不被请求默认值绕过。
        sample_cap = min(max_samples, self._max_samples)

        snapshot = self._store.read_stable_snapshot(after_sequence)
        # 不信任未经核对的 head 来缩小验证窗口（R4）。核对分两段：genesis 固定值
        # 在扫描前核对（空链没有记录可扫）；非 genesis 的 head 与末记录一致性放到
        # 扫描之后核对——记录自身被篡改时（§12.3/§12.4），先让页扫描按证据归
        # incomplete/tampered，head 比对不抢先抛错掩盖状态；链身完好而 head 失配
        # 才是 head 被伪造。
        self._check_genesis_head(snapshot)
        through = snapshot.head.sequence

        checked = 0
        errors = 0
        samples: list[Proof] = []
        truncated = False
        high_water = 0
        min_epoch = 0
        max_epoch = 0
        saw_tampered = False
        saw_incomplete = False
        # 首次坏记录/缺口后冻结连续高水位（PR3-09）：high_water_mark 语义是「本次
        # **连续**成功验证到的最高 sequence」，不能越过坏记录推进。
        high_water_frozen = False

        prev_digest = GENESIS_DIGEST
        prev_seq = 0
        # 末条已验证记录的 key ref：扫描判 clean 后与 head 复核用（R4）。
        prev_key_id = ""
        prev_key_epoch = 0
        # 从非零 after_sequence 起验时，前序摘要无法从链首重建：checkpoint proof 只做
        # 自洽校验（不验链式链接），其 digest 作为后续记录的续链基线（范围验证的诚实
        # 边界）。checkpoint 校验通过即推进 high_water_mark（F05 §6.1）。
        if after_sequence > 0:
            checkpoint = snapshot.checkpoint
            if checkpoint is None:
                # checkpoint 不存在：incomplete，不回落 genesis、不跳到下一条。
                anchor_state = self._verify_anchor(snapshot, anchor_policy)
                return AuditVerificationResult(
                    status=AuditIntegrityStatus.INCOMPLETE,
                    checked_count=0,
                    error_count=0,
                    truncated=False,
                    high_water_mark=0,
                    key_epoch_range=(0, 0),
                    anchor=anchor_state,
                    detail=f"checkpoint sequence {after_sequence} not found in stable snapshot",
                )
            proof = checkpoint.proof
            checked += 1
            min_epoch, max_epoch = _extend_epoch_range(min_epoch, max_epoch, proof.key_epoch)
            status = self._verify_one(checkpoint, link_check=False)
            if status is AuditIntegrityStatus.CLEAN:
                if not high_water_frozen:
                    high_water = proof.sequence
            else:
                high_water_frozen = True
                errors += 1
                saw_tampered, saw_incomplete, truncated = _record_error(
                    status, proof, samples, sample_cap, (saw_tampered, saw_incomplete, truncated)
                )
            prev_digest = proof.digest
            prev_seq = proof.sequence
            prev_key_id = proof.key_id
            prev_key_epoch = proof.key_epoch

        cursor = after_sequence
        while cursor < through:
            page_records = self._store.scan(cursor, page, through_sequence=through)
            if not page_records:
                # 页空且尚未到达链头：缺记录或截断，拒绝当 clean（F05 §6.1）。
                saw_incomplete = True
                break
            for record in page_records:
                proof = record.proof
                checked += 1
                min_epoch, max_epoch = _extend_epoch_range(min_epoch, max_epoch, proof.key_epoch)

                status = self._verify_one(
                    record, link_check=True, prev_digest=prev_digest, prev_seq=prev_seq
                )
                if status is AuditIntegrityStatus.CLEAN:
                    if not high_water_frozen:
                        high_water = proof.sequence
                else:
                    high_water_frozen = True
                    errors += 1
                    saw_tampered, saw_incomplete, truncated = _record_error(
                        status,
                        proof,
                        samples,
                        sample_cap,
                        (saw_tampered, saw_incomplete, truncated),
                    )
                # 无论本条结果如何，前序游标都推进：后续记录的链接校验基于实际前序。
                prev_digest = proof.digest
                prev_seq = proof.sequence
                prev_key_id = proof.key_id
                prev_key_epoch = proof.key_epoch
                cursor = proof.sequence
            if cursor >= through:
                break

        # 扫描判 clean 后再核对 head 与末条已验证记录（R4）：链身证据全部通过而
        # head 与其不一致，唯一解释是 head 自身被伪造——缩链（记录仍在 head 之上，
        # 扫描窗口被 head 缩小）或 digest/key ref 篡改。同步删尾并更新 head 时两方
        # 一致，不会误报（§12.3 无锚点不误报回滚）。
        if not saw_tampered and not saw_incomplete and cursor >= through:
            self._check_head_against_scan(
                snapshot, prev_seq, prev_digest, prev_key_id, prev_key_epoch
            )

        anchor_state = self._verify_anchor(snapshot, anchor_policy)
        status = self._aggregate_status(
            saw_tampered=saw_tampered,
            saw_incomplete=saw_incomplete,
            reached_head=cursor >= through,
            anchor_state=anchor_state,
        )
        return AuditVerificationResult(
            status=status,
            checked_count=checked,
            error_count=errors,
            truncated=truncated,
            high_water_mark=high_water,
            key_epoch_range=(min_epoch, max_epoch),
            anchor=anchor_state,
            samples=tuple(samples),
        )

    # -- 内部：签发 / 单条验证 / 锚点 / 聚合 ------------------------------ #

    @staticmethod
    def _check_genesis_head(snapshot: ChainSnapshot) -> None:
        """核对空链 genesis head 的固定值（R4，计划 §7.1）。

        sequence=0 且快照无记录的 head 必须是 :data:`GENESIS_DIGEST` + 空 key ref +
        当前格式版本。head 缩回 genesis 而快照仍有记录的伪造由扫描后的
        :meth:`_check_head_against_scan` 兜住（扫描窗口被 head 缩小时证据仍在）。
        """
        head = snapshot.head
        if head.sequence != 0 or snapshot.last_record is not None:
            return
        if not hmac.compare_digest(head.digest, GENESIS_DIGEST):
            raise AuditSchemaError("genesis chain head digest != GENESIS_DIGEST")
        if head.key_id != "" or head.key_epoch != 0:
            raise AuditSchemaError("genesis chain head must carry an empty key ref")
        if head.format_version != CANONICAL_FORMAT_VERSION:
            raise AuditSchemaError("genesis chain head format version is not canonical")

    @staticmethod
    def _check_head_against_scan(
        snapshot: ChainSnapshot,
        prev_seq: int,
        prev_digest: str,
        prev_key_id: str,
        prev_key_epoch: int,
    ) -> None:
        """clean 扫描后核对 head 与末条已验证记录（R4，计划 §7.1）。

        末条记录的 HMAC 已通过验证，其 digest/key ref 可信；head 与之失配即 head
        被伪造。缩链（last_record 的 sequence 仍在 head 之上）单独立判——那是
        「head 被改小、扫描窗口随之缩小」的回滚形态。
        """
        head = snapshot.head
        last = snapshot.last_record
        if last is not None and last.proof.sequence > head.sequence:
            raise AuditSchemaError(f"records above chain head sequence {head.sequence}")
        if head.sequence != prev_seq or not hmac.compare_digest(head.digest, prev_digest):
            raise AuditSchemaError("chain head does not match the last verified record")
        if head.key_id != prev_key_id or head.key_epoch != prev_key_epoch:
            raise AuditSchemaError("chain head key ref does not match the last verified record")

    def _sign(self, event: AuditEvent, *, sequence: int, previous_digest: str) -> Proof:
        """用活动 key 签发单条 proof（处理轮换竞态，计划 §5.2）。"""
        for _ in range(self._max_key_retries):
            ref = self._keys.active_key()
            message = canonical_event_bytes(
                event, sequence=sequence, previous_digest=previous_digest, key_ref=ref
            )
            tag, used = self._keys.mac(message, purpose=AUDIT_MAC_PURPOSE)
            if used == ref:
                return Proof(
                    format_version=CANONICAL_FORMAT_VERSION,
                    sequence=sequence,
                    previous_digest=previous_digest,
                    digest=tag.hex(),
                    key_id=ref.key_id,
                    key_epoch=ref.epoch,
                )
            # 活动密钥在 active_key() 与 mac() 之间被轮换：用返回的 ref 重算 canonical。
            ref = used
            message = canonical_event_bytes(
                event, sequence=sequence, previous_digest=previous_digest, key_ref=ref
            )
            tag, used = self._keys.mac(message, purpose=AUDIT_MAC_PURPOSE)
            if used == ref:
                return Proof(
                    format_version=CANONICAL_FORMAT_VERSION,
                    sequence=sequence,
                    previous_digest=previous_digest,
                    digest=tag.hex(),
                    key_id=ref.key_id,
                    key_epoch=ref.epoch,
                )
        raise KeyCapabilityError("active audit key changed repeatedly during signing")

    def _verify_one(
        self,
        record: ChainedRecord,
        *,
        link_check: bool,
        prev_digest: str = "",
        prev_seq: int = 0,
    ) -> AuditIntegrityStatus:
        """校验单条记录的 proof 自洽与链式链接（计划 §12.1、§12.3）。

        ``link_check=False`` 只做 proof 自洽（增量验证的 checkpoint：其前序在本次范围
        之外，无法从链首重建）。
        """
        proof = record.proof
        if proof.format_version != CANONICAL_FORMAT_VERSION:
            # 未知格式版本返回 incomplete，拒绝当 clean（计划 §4.5）。
            return AuditIntegrityStatus.INCOMPLETE
        if not proof.digest or not proof.key_id:
            # 空 digest / key_id 是「无 proof 行」（普通记录或被剥离 proof 的行）的
            # sentinel：证据不足归 incomplete，不当 clean 也不猜 tampered。
            return AuditIntegrityStatus.INCOMPLETE
        # 链接校验：sequence 单调 +1、previous_digest 指向前序。
        if link_check:
            if proof.sequence != prev_seq + 1:
                # 缺记录（gap）归 incomplete，乱序归 tampered。
                if proof.sequence > prev_seq + 1:
                    return AuditIntegrityStatus.INCOMPLETE
                return AuditIntegrityStatus.TAMPERED
            if proof.previous_digest != prev_digest:
                return AuditIntegrityStatus.TAMPERED
        # proof 自洽：用 proof 自带 KeyRef 重算并常时间比较。
        ref = KeyRef(key_id=proof.key_id, epoch=proof.key_epoch)
        message = canonical_event_bytes(
            record.event,
            sequence=proof.sequence,
            previous_digest=proof.previous_digest,
            key_ref=ref,
        )
        try:
            digest_bytes = bytes.fromhex(proof.digest)
        except ValueError:
            return AuditIntegrityStatus.TAMPERED
        try:
            ok = self._keys.verify_mac(message, digest_bytes, purpose=AUDIT_MAC_PURPOSE, ref=ref)
        except KeyMismatchError:
            # 该 epoch 的历史验证材料不可用：incomplete，不回退活动 key（计划 §5.2）。
            return AuditIntegrityStatus.INCOMPLETE
        if not ok:
            return AuditIntegrityStatus.TAMPERED
        return AuditIntegrityStatus.CLEAN

    def _verify_anchor(self, snapshot: ChainSnapshot, anchor_policy: str) -> AnchorState:
        """核对外部锚点（计划 §6）。无锚点时诚实声明 ``checked=False``，不宣称防回滚。

        ``status`` 必须是冻结契约的 :class:`AnchorStatus` 枚举（PR3-05：裸字符串违反
        ``AnchorState`` 构造校验）。本地链长于锚点（lagging）时还须核对锚定位置的
        真实前缀 digest——锚点代表「链在该历史位置的证据」，本地对应位置的记录被删除、
        篡改或与锚点不符，都是回滚/篡改嫌疑，报 conflict，不得只看 head 是否更靠后。
        """
        if anchor_policy == "skip":
            return AnchorState(checked=False)
        if self._anchor is None:
            if anchor_policy == "required":
                raise ValidationError(
                    "anchor_policy=required 但未配置外部锚点；无法满足 required 锚点核对"
                )
            return AnchorState(checked=False)
        try:
            anchored = self._anchor.read_anchored(chain_id=self._chain_id)
        except Exception:
            _LOG.error("audit anchor 读取失败", exc_info=True)
            return AnchorState(checked=True, status=AnchorStatus.UNAVAILABLE)
        if anchored is None:
            return AnchorState(checked=True, status=AnchorStatus.UNAVAILABLE)
        head = snapshot.head
        if anchored.sequence > head.sequence:
            # 本地链短于可信锚点：rollback-suspected（计划 §6）。
            return AnchorState(checked=True, status=AnchorStatus.CONFLICT)
        if anchored.sequence == head.sequence:
            if not hmac.compare_digest(anchored.digest, head.digest):
                return AnchorState(checked=True, status=AnchorStatus.CONFLICT)
            return AnchorState(checked=True, status=AnchorStatus.OK)
        # 本地链长于锚点：核对锚定位置的本地前缀证据，匹配才是真 lagging（PR3-05）。
        local_digest = self._local_digest_at(anchored.sequence, snapshot)
        if local_digest is None or not hmac.compare_digest(local_digest, anchored.digest):
            return AnchorState(checked=True, status=AnchorStatus.CONFLICT)
        return AnchorState(checked=True, status=AnchorStatus.LAGGING)

    def _local_digest_at(self, sequence: int, snapshot: ChainSnapshot) -> str | None:
        """读取本地链第 ``sequence`` 条记录的 proof digest。

        返回 ``None`` 表示该位置无记录（被删除/截断）。``sequence=0`` 对应 genesis
        条件：空链的前序摘要固定为 :data:`GENESIS_DIGEST`。
        """
        if sequence <= 0:
            return GENESIS_DIGEST if sequence == 0 else None
        page = self._store.scan(sequence - 1, 1, through_sequence=snapshot.head.sequence)
        if not page or page[0].proof.sequence != sequence:
            return None
        return page[0].proof.digest

    @staticmethod
    def _aggregate_status(
        *,
        saw_tampered: bool,
        saw_incomplete: bool,
        reached_head: bool,
        anchor_state: AnchorState,
    ) -> AuditIntegrityStatus:
        if anchor_state.status is AnchorStatus.CONFLICT:
            return AuditIntegrityStatus.ROLLBACK_SUSPECTED
        if saw_tampered:
            return AuditIntegrityStatus.TAMPERED
        if saw_incomplete or not reached_head:
            # 页空、缺口、截断或未达快照链头：拒绝把截断前缀报告为 clean。
            return AuditIntegrityStatus.INCOMPLETE
        # checked==0 的空链（head.sequence=0）同样落在这里：范围内无记录也无矛盾。
        return AuditIntegrityStatus.CLEAN


def _record_error(
    status: AuditIntegrityStatus,
    proof: Proof,
    samples: list[Proof],
    sample_cap: int,
    flags: tuple[bool, bool, bool],
):
    """登记一条非 clean 记录：按实际状态更新旗标并把 proof 收进有界样本。"""
    saw_tampered, saw_incomplete, truncated = flags
    if status is AuditIntegrityStatus.TAMPERED:
        saw_tampered = True
    else:
        saw_incomplete = True
    if len(samples) < sample_cap:
        samples.append(proof)
    else:
        truncated = True
    return saw_tampered, saw_incomplete, truncated


def _extend_epoch_range(min_e: int, max_e: int, epoch: int) -> tuple[int, int]:
    if min_e == 0 or epoch < min_e:
        min_e = epoch
    if epoch > max_e:
        max_e = epoch
    return min_e, max_e


# ====================================================================== #
# 注册
# ====================================================================== #


@AuditIntegrityProducer.register("chained_hmac")
def _build(config) -> ChainedHmacAuditIntegrityProvider:
    """装配 chained_hmac 审计完整性 provider（计划 §8.1）。

    ``key_provider`` 与 ``audit``（ChainStore）均为**具名引用**，由 Factory 共享缓存保证
    provider 持有的 store 与 AuditLogger 是同一具名实例（计划 §4.1）。``max_cas_retries``
    / ``verify_page_size`` 等为非敏感调参，可内联配置。
    """
    from jiuwen_memory.common.audit.base import AuditProducer

    key_provider = _resolve_key_provider(config)
    store = AuditProducer.dep(config, "audit")
    if not isinstance(store, ChainedAuditStore):
        raise ValidationError(
            "audit_integrity 引用的 audit 后端必须同时实现 ChainedAuditStore；"
            f"得到 {type(store).__name__}"
        )
    return ChainedHmacAuditIntegrityProvider(
        store,
        key_provider,
        max_cas_retries=int(Factory.cfg_get(config, "max_cas_retries", DEFAULT_MAX_CAS_RETRIES)),
        max_key_retries=int(Factory.cfg_get(config, "max_key_retries", DEFAULT_MAX_KEY_RETRIES)),
        verify_page_size=int(
            Factory.cfg_get(config, "verify_page_size", DEFAULT_AUDIT_VERIFY_PAGE_SIZE)
        ),
        max_samples=int(Factory.cfg_get(config, "max_samples", DEFAULT_AUDIT_VERIFY_MAX_SAMPLES)),
        chain_id=str(Factory.cfg_get(config, "chain_id", "default")),
    )


def _resolve_key_provider(config) -> KeyProvider:
    """取审计专用具名 KeyProvider（计划 §5.1）。

    审计 key 必须独立具名，不得与 ``primary_crypto`` 共享同一活动 key 实例或 key 文件--
    这条隔离由配置层（不同具名实例 + 不同 key_file）表达，装配期无法完全自动强制，故
    在文档与示例配置中明确要求。
    """
    from jiuwen_memory.common.security.cryptography.key_provider import KeyProviderProducer

    key_provider = KeyProviderProducer.dep(config, "key_provider")
    if not isinstance(key_provider, KeyProvider):
        raise ValidationError("audit_integrity 的 key_provider 必须是 KeyProvider 实现")
    return key_provider
