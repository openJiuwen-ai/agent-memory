"""审计日志链式 HMAC 完整性保护（security.md §7.3）。

链式 HMAC 让「改一行内容 = 该行 HMAC 对不上（需 key 才能重算修复）」。它检出
中间行内容篡改，但**不**检出删除尾部或回滚到旧快照（剩余记录 HMAC 仍自洽）--
完整防篡改需外部可信锚点，不在本期（负责人批准范围降级，见 F03 遗留 1）。

**实现方式：装饰器**。``HmacAuditLogger`` 包住任意 :class:`~common.audit.base.AuditLogger`，
在 ``record`` 时算链式 HMAC 塞进 ``event.detail`` 再委托给被包的 logger；``verify_integrity``
流式校验返回被篡改的行索引。装饰器通过 ``record_chained`` / ``get_chain_head`` 让持久化
后端做事务 CAS（多实例不分叉），普通后端降级为单实例锁。

**key 来源**：从 :class:`LocalKeyProvider` 的 Encryption Root Key 经 HKDF 派生
（``derive_audit_key``，context=``audit``）。派生 ``info`` 是公开常量，不构成安全门槛--
root key 泄漏后攻击者可派生 audit key 重算任意链（完整解需独立 audit key/KMS，见 F03 遗留）。
轮换根密钥会让历史链无法验证（当前无 key_id/epoch，见 F03 遗留 2）。

**序列化**：HMAC 覆盖稳定规范化后的全部字段。``AuditEvent.detail`` 是 ``dict[str,str]``，
用 ``json.dumps(..., sort_keys=True, separators=...)`` 稳定序列化；其它字段是 ``str`` /
``Scope``（frozen）/ ``datetime``（isoformat）。不直接签裸 JSON 又用另一种序列化验证。
"""

from __future__ import annotations

import hashlib
import hmac
import json

from common.audit.base import AuditIntegrityResult, AuditLogger, AuditProducer
from common.errors import ValidationError
from common.type_def import AuditEvent

# 派生 audit HMAC key 的 HKDF info。与 org KEK 的 info 前缀区分，确保派生密钥隔离。
_AUDIT_KEY_INFO = b"agent-memory:security:audit-hmac:v1"
# 链式 HMAC 的算法。SHA256 足够，且与 key 派生同族。
_HMAC_ALGO = hashlib.sha256


def derive_audit_key(root_key: bytes) -> bytes:
    """从 Encryption Root Key 派生 audit HMAC key。

    HKDF（extract-then-expand）派生：同 root key 派生出同一 audit key；不同 root key
    派生不同。派生后与原 key 不同（隔离），root key 泄漏不等于 audit key 立即可用
    （仍需知道派生参数，且 root key 本就是最高敏感）。

    用 HKDF 而非直接 ``hmac(key, b"audit")``：HKDF 是标准 KDF，提供 extract 阶段
    消除 root key 的统计偏差，expand 阶段绑死 context。``cryptography`` 已是主依赖。
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,  # root key 自身已是高熵密钥，extract salt 可省
        info=_AUDIT_KEY_INFO,
    ).derive(root_key)


def _canonical_event_bytes(event: AuditEvent) -> bytes:
    """事件的稳定规范序列化，用于 HMAC 计算。

    必须覆盖全部参与审计的字段，且序列化是**确定性**的（同事件恒同字节）。
    ``detail`` 里的 ``_hmac`` / ``prev_hmac`` 在计算时**排除**（它们是 HMAC 的产物，
    含进去会自指）。``Scope`` 是 frozen，按字段顺序展开。``datetime`` 用 isoformat。
    """
    payload = {
        "id": event.id,
        "actor": _scope_dict(event.actor),
        "action": event.action,
        "target_id": event.target_id,
        "layer": event.layer,
        "decision": event.decision,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else "",
        "target": _scope_dict(event.target),
        "detail": {k: v for k, v in event.detail.items() if k not in ("_hmac", "prev_hmac")},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _scope_dict(scope) -> dict[str, str]:
    return {
        "org": scope.org,
        "space": getattr(scope, "space", ""),
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


class HmacAuditLogger(AuditLogger):
    """给任意 :class:`AuditLogger` 加链式 HMAC 完整性保护。

    ``record`` 算出本条的 HMAC（含前一条的 HMAC）后写入 ``event.detail["_hmac"]`` 与
    ``["prev_hmac"]``，再委托给被包的 logger。``verify_integrity`` 流式重算比对，
    返回被篡改的行索引。

    链状态（``_prev_hmac``）是实例级。构造时从持久化后端读最后一条的 ``_hmac`` 作为
    初值（见 ``_recover_chain_head``），进程重启后续接旧链而非从空开始。
    """

    def __init__(
        self, delegate: AuditLogger, *, hmac_key: bytes, verify_on_start: bool = True
    ) -> None:
        if not hmac_key:
            raise ValueError("hmac_key 不可为空")
        self._delegate = delegate
        self._key = hmac_key
        # 链头锁：覆盖「读链头 -> 算 HMAC -> 委托追加 -> 更新链头」完整区间（审计 P1-1）。
        # SqliteAuditLogger 的内部 RLock 只保护单次 SQLite 写，保护不了装饰器外层已
        # 发生的链头读取。ThreadingHTTPServer 每请求一线程，并发 record 不加锁会分叉链。
        import threading

        self._lock = threading.Lock()
        # 从持久化后端恢复链头（审计 P1-1/P1-2）：含旧库迁移 + head 一致性校验。
        self._prev_hmac: str = self._recover_chain_head()
        # 启动验证（审计 P1-2）：持久化后端有历史事件时验证链完整性，坏库拒绝启动。
        # verify_on_start=False 仅限测试/迁移；真文件持久化由 _enforce_audit_integrity
        # 强制不可绕过（审计 P1-3）。
        if verify_on_start:
            self._verify_on_start()

    def _recover_chain_head(self) -> str:
        """恢复链头，含旧库迁移与 head 一致性校验（审计 P1-1/P1-2）。

        用 ``get_chain_state`` 稳定快照（同一锁内读 head + 最后事件，审计 P1-2b），
        避免两次独立读取间被并发追加插入不一致状态。

        三种情况：
        1. 全新库（无事件）：head 空，正常。
        2. 旧版库（有事件、chain-head 行不存在或缺 last_seq）：验证旧链后，初始化 head
           为最后一条 _hmac + 真实 seq（迁移回填 last_seq，审计 P1-2a）。未签名/验证失败
           拒绝迁移。
        3. 新版库（有事件、chain-head 完整）：比对 head_hmac == last_event_hmac 且
           head_last_seq == last_event_seq（审计 P1-2），不一致拒绝启动。
        """
        head_hmac, head_last_seq, last_seq, last_hmac = self._delegate.get_chain_state()
        if last_seq == 0 and not last_hmac:
            return head_hmac  # 全新库或内存后端无历史
        # 旧库迁移：chain-head 行不存在（head_hmac 空）或 last_seq 缺失（紧邻上一版 schema）
        if not head_hmac or head_last_seq == 0:
            if not last_hmac:
                return head_hmac  # 有事件但无 _hmac = 未签名库，_verify_on_start 会拒
            # 验证旧链完整后才初始化 head（回填真实 last_seq，审计 P1-2a）
            result = self.verify_integrity()
            if result.status == "tampered":
                raise ValidationError(
                    f"旧库迁移失败：检测到 {len(result.tampered_indices)} 行篡改，"
                    "拒绝初始化 chain-head。"
                )
            if hasattr(self._delegate, "init_chain_head"):
                self._delegate.init_chain_head(last_hmac, last_seq)
            return last_hmac
        # head 一致性校验（P1-2）：head 与最后事件的 seq + HMAC 都须一致
        if head_hmac != last_hmac or head_last_seq != last_seq:
            raise ValidationError(
                "audit chain-head 与最后事件不一致（head_hmac/last_seq vs 事件）："
                "chain-head 表可能被篡改/损坏或并发追加未同步。拒绝启动。"
            )
        return head_hmac

    def _verify_on_start(self) -> None:
        """启动验证（审计 P1-2）：有历史时验证链完整性，坏库拒绝启动。fail-fast。"""
        history = self._delegate.tail(1)
        if not history:
            return
        # fail-fast：遇首个错误即拒，不收集全部（审计 P2-1）。keyset 分页（iter_chain）。
        page_size = 1000
        prev_hmac = ""
        after_seq = 0
        while True:
            page = self._delegate.iter_chain(after_seq=after_seq, limit=page_size)
            if not page:
                break
            for seq, event in page:
                stored_hmac = event.detail.get("_hmac", "")
                stored_prev = event.detail.get("prev_hmac", "")
                if not hmac.compare_digest(stored_prev, prev_hmac):
                    raise ValidationError("audit chain 启动验证失败：prev_hmac 链断裂。拒绝启动。")
                expected = hmac.new(
                    self._key,
                    stored_prev.encode("utf-8") + _canonical_event_bytes(event),
                    _HMAC_ALGO,
                ).hexdigest()
                if not hmac.compare_digest(stored_hmac, expected):
                    raise ValidationError(
                        "audit chain 启动验证失败：事件 HMAC 不自洽。拒绝启动--已有日志不可信。"
                    )
                prev_hmac = stored_hmac
                after_seq = seq
            if len(page) < page_size:
                break

    def record(self, event: AuditEvent) -> None:
        # 多实例/多连接原子性（审计 P1-1）：用 delegate 的 record_chained 事务 CAS。
        # 持久化后端在 BEGIN IMMEDIATE 下「读 head - 验证 == 期望 - 追加 - 提交 head」，
        # 两实例同时写同一库时，后到者的 expected_head 对不上 -> ConflictError -> 重试。
        # 单实例内存后端无事务，record_chained 降级为 record + 实例锁（本方法仍持锁）。
        from dataclasses import replace

        from common.errors import ConflictError

        with self._lock:
            for _ in range(10):  # CAS 重试上限，避免活锁
                prev = self._prev_hmac
                digest = hmac.new(
                    self._key,
                    prev.encode("utf-8") + _canonical_event_bytes(event),
                    _HMAC_ALGO,
                ).hexdigest()
                enriched = dict(event.detail)
                enriched["prev_hmac"] = prev
                enriched["_hmac"] = digest
                try:
                    self._delegate.record_chained(
                        replace(event, detail=enriched), expected_head=prev
                    )
                    self._prev_hmac = digest
                    return
                except ConflictError:
                    # 另一实例先写入，链头已变 -> 重读 head 重试
                    self._prev_hmac = self._delegate.get_chain_head()
            raise ConflictError(
                "audit chain CAS 重试超限：多实例竞争激烈，请减少并发 writer 或用单 writer"
            )

    def query(
        self, filters: dict[str, str], limit: int = 100, *, offset: int = 0
    ) -> list[AuditEvent]:
        return self._delegate.query(filters, limit, offset=offset)

    def verify_integrity(self) -> AuditIntegrityResult:
        """流式校验审计链完整性，返回结构化结果（审计 P2-1/P2-2）。

        keyset 分页（``iter_chain`` 的 ``WHERE seq > ?``，O(limit)/页），只保留上一条
        HMAC--内存上界与日志总量无关。结果返回 ``AuditIntegrityResult``，区分
        clean/tampered（不再以空列表 fail-open）。

        §7.3「改一行 = 破坏该行及后续」：攻击者改行后不重算 HMAC -> 该行检出；重算
        （需 key）-> 该行 _hmac 变 -> 下一条 prev_hmac 对不上 -> 检出。没 key 就修不好。

        **已知局限（审计 P1-3，实现侧建议待负责人确认）**：本地链式 HMAC 无法检测「删除
        日志尾部」或「回滚到旧快照」--剩余记录的 HMAC 仍全部自洽。完整防篡改需外部可信
        锚点（WORM/远端/KMS），见 F03 范围降级声明。
        """
        page_size = 1000
        max_samples = 100  # 篡改采样上限（审计 P2-1），防大量坏行线性耗尽内存
        tampered: list[int] = []
        tampered_count = 0
        prev_hmac = ""
        after_seq = 0
        global_idx = 0
        while True:
            page = self._delegate.iter_chain(after_seq=after_seq, limit=page_size)
            if not page:
                break
            for seq, event in page:
                stored_hmac = event.detail.get("_hmac", "")
                stored_prev = event.detail.get("prev_hmac", "")
                bad = False
                if not hmac.compare_digest(stored_prev, prev_hmac):
                    bad = True
                expected = hmac.new(
                    self._key,
                    stored_prev.encode("utf-8") + _canonical_event_bytes(event),
                    _HMAC_ALGO,
                ).hexdigest()
                if not hmac.compare_digest(stored_hmac, expected):
                    bad = True
                if bad:
                    tampered_count += 1
                    if len(tampered) < max_samples:
                        tampered.append(global_idx)
                prev_hmac = stored_hmac
                global_idx += 1
                after_seq = seq
            if len(page) < page_size:
                break
        status = "tampered" if tampered_count else "clean"
        return AuditIntegrityResult(status=status, tampered_indices=tampered, checked=True)


@AuditProducer.register("hmac")
def _build(config):
    """配置驱动：``audit.default.target: hmac`` 时包一层 HmacAuditLogger。

    必须配 ``inner``（被包的 audit logger，如 sqlite）。HMAC key 从 Encryption
    Root Key 派生：用与加密同源的 key 配置（``key_file`` / ``key_hex`` / ``key_env``）
    构造 ``LocalKeyProvider`` 取 root key，再 ``derive_audit_key`` 派生。配置同源
    即 key 同源，无需单独管理 audit key。
    """
    from common.security.security_impl.local_envelope_security_provider import (
        LocalKeyProvider,
    )

    inner_name = config.params.get("inner")
    if not inner_name:
        raise ValidationError("audit 'hmac' 必须配置 inner（被包的 audit logger）")
    # 自引用/循环检测（审计 P3-3）：inner 指向自身会 RecursionError 崩溃。
    if isinstance(inner_name, str) and inner_name == config.name:
        raise ValidationError(f"audit 'hmac' 的 inner 不能指向自身（{inner_name!r}），会循环装配")
    inner = AuditProducer.dep(config, "inner")
    root_key = LocalKeyProvider(
        key_file=config.params.get("key_file", ""),
        key_hex=config.params.get("key_hex", ""),
        key_b64=config.params.get("key_b64", ""),
        key_env=config.params.get("key_env", ""),
    ).get_encryption_root_key()
    # verify_on_start 不可通过配置关闭（审计 P1-3）：生产持久化必须启动验证，
    # 否则坏库/未签名库仍启动。测试/迁移用 HmacAuditLogger(..., verify_on_start=False)
    # 直接构造，不走本工厂。忽略配置里的 verify_on_start 值（防 bool("false")=True 误设）。
    return HmacAuditLogger(inner, hmac_key=derive_audit_key(root_key), verify_on_start=True)
