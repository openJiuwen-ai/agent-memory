# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ProtectedAuditLogger - record 走完整性链、query 透传的契约（F05 §Audit Integrity）。

接口先行版：wrapper 属纯委托契约层，用 stub provider / stub logger 验证委托方向，
不含任何密码学。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.audit import ProtectedAuditLogger
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityError,
    AuditIntegrityProvider,
    AuditVerificationResult,
    ChainConflictError,
    Proof,
)
from jiuwen_memory.common.security.audit_integrity.chain_store import (
    GENESIS_DIGEST,
    ChainedRecord,
)
from jiuwen_memory.common.type_def import AuditEvent

pytestmark = pytest.mark.unit


class _StubProvider(AuditIntegrityProvider):
    def __init__(self, store) -> None:
        self.store = store
        self.recorded = []

    def chain_store(self):
        return self.store

    def capabilities(self):
        raise NotImplementedError

    def record_chained(self, event: AuditEvent) -> ChainedRecord:
        self.recorded.append(event)
        return ChainedRecord(
            event=event,
            proof=Proof(
                format_version=1,
                sequence=len(self.recorded),
                previous_digest=GENESIS_DIGEST,
                digest="a" * 64,
                key_id="kp-1",
                key_epoch=1,
            ),
        )

    def verify(self, **kwargs) -> AuditVerificationResult:
        raise NotImplementedError

    def active_key_ref(self):
        raise NotImplementedError

    def health(self) -> None:
        return None


class _StubLogger:
    def __init__(self) -> None:
        self.events = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        return self.events[:limit]


def test_record_delegates_to_integrity_provider() -> None:
    """record 经 provider 记链（带 proof），不再直连底层 AuditLogger。"""
    logger = _StubLogger()
    provider = _StubProvider(logger)
    wrapper = ProtectedAuditLogger(provider, logger)

    event = AuditEvent(action="add", decision="allow")
    wrapper.record(event)

    assert provider.recorded == [event]
    assert logger.events == []


def test_query_passes_through_underlying_logger() -> None:
    """query 透传底层后端（链式模式下读 chain 记录，脱 proof）。"""
    logger = _StubLogger()
    provider = _StubProvider(logger)
    logger.events.append(AuditEvent(action="add", decision="allow"))
    wrapper = ProtectedAuditLogger(provider, logger)

    events = wrapper.query({"action": "add"}, limit=10)

    assert [event.action for event in events] == ["add"]


def test_wrapper_is_an_audit_logger() -> None:
    """wrapper 可直接替换任何 AuditLogger 注入点。"""
    from jiuwen_memory.common.audit.base import AuditLogger

    logger = _StubLogger()
    assert isinstance(ProtectedAuditLogger(_StubProvider(logger), logger), AuditLogger)


def test_wrapper_rejects_provider_and_logger_backed_by_different_stores() -> None:
    """关键装配不变量用对象 identity 验证，不能只靠注释或同名配置。"""
    with pytest.raises(ValidationError, match="same store instance"):
        ProtectedAuditLogger(_StubProvider(_StubLogger()), _StubLogger())


# ====================================================================== #
# detail 统一隐私过滤（R8a，计划 §10：写入边界，过滤后再进 proof）
# ====================================================================== #


def test_record_redacts_sensitive_detail_values() -> None:
    """
    敏感键词干（不区分大小写、包含匹配）的值整值替换为固定标记，
    不进链、不进存储（R8a）。
    """
    logger = _StubLogger()
    provider = _StubProvider(logger)
    wrapper = ProtectedAuditLogger(provider, logger)

    event = AuditEvent(
        action="add",
        decision="allow",
        detail={
            "Api_Key": "sk-live-1234",
            "auth_token": "eyJhbGci",
            "user_password": "hunter2",
            "client_secret": "s3cr3t",
            "key_hex": "55" * 32,
            "role": "member",
        },
    )
    wrapper.record(event)

    stored = provider.recorded[0].detail
    assert stored["Api_Key"] == "[redacted]"
    assert stored["auth_token"] == "[redacted]"
    assert stored["user_password"] == "[redacted]"
    assert stored["client_secret"] == "[redacted]"
    assert stored["key_hex"] == "[redacted]"
    assert stored["role"] == "member"


def test_record_truncates_overlong_values_and_bounds_key_count() -> None:
    """单值超长截断、键数超限丢尾：detail 有界，不得成为签名/存储 DoS（R8a）。"""
    logger = _StubLogger()
    provider = _StubProvider(logger)
    wrapper = ProtectedAuditLogger(provider, logger)

    detail = {"long": "x" * 5000, **{f"k{i}": str(i) for i in range(70)}}
    wrapper.record(AuditEvent(action="add", decision="allow", detail=detail))

    stored = provider.recorded[0].detail
    assert len(stored) == 64  # 70 个键截到上限
    assert stored["long"] == "x" * 1024 + "...[truncated]"


def test_record_str_normalizes_non_string_values() -> None:
    """非 str 值统一 str 化（与 canonical 序列化语义一致），存储不出现混合类型。"""
    logger = _StubLogger()
    provider = _StubProvider(logger)
    wrapper = ProtectedAuditLogger(provider, logger)

    wrapper.record(AuditEvent(action="add", decision="allow", detail={"count": 3, "ratio": 0.5}))

    stored = provider.recorded[0].detail
    assert stored["count"] == "3"
    assert stored["ratio"] == "0.5"


def test_record_empty_detail_event_passes_through_unchanged() -> None:
    """空 detail 的事件原样透传（对象 identity 不变），不过滤不重建。"""
    logger = _StubLogger()
    provider = _StubProvider(logger)
    wrapper = ProtectedAuditLogger(provider, logger)

    event = AuditEvent(action="add", decision="allow")
    wrapper.record(event)

    assert provider.recorded[0] is event


# ====================================================================== #
# 统一失败语义与 degraded 闩（R1，计划 §9.3）
# ====================================================================== #


class _FailingProvider(_StubProvider):
    """record_chained 按构造注入抛错：模拟后端/密钥/序列化故障。"""

    def __init__(self, store, exc: Exception) -> None:
        super().__init__(store)
        self.exc = exc

    def record_chained(self, event: AuditEvent) -> ChainedRecord:
        raise self.exc


def test_record_normalizes_non_integrity_failure_and_latches() -> None:
    """
    非完整性族的原生异常（如 SQLite 磁盘故障）归一为 AuditIntegrityError
    上抛（R1），原始异常挂 __cause__ 可追溯，degraded 闩置位。
    """
    logger = _StubLogger()
    failure = RuntimeError("database is locked")
    wrapper = ProtectedAuditLogger(_FailingProvider(logger, failure), logger)

    with pytest.raises(AuditIntegrityError, match="RuntimeError"):
        wrapper.record(AuditEvent(action="add", decision="allow"))

    assert wrapper.integrity_degraded is True


def test_record_integrity_failure_propagates_and_latches() -> None:
    """完整性族异常原样传播、同样置位 degraded 闩。"""
    logger = _StubLogger()
    failure = ChainConflictError("audit chain append CAS retry exhausted")
    wrapper = ProtectedAuditLogger(_FailingProvider(logger, failure), logger)

    with pytest.raises(ChainConflictError):
        wrapper.record(AuditEvent(action="add", decision="allow"))

    assert wrapper.integrity_degraded is True


def test_healthy_record_keeps_latch_clear() -> None:
    """成功写入不置位 degraded 闩。"""
    logger = _StubLogger()
    wrapper = ProtectedAuditLogger(_StubProvider(logger), logger)

    wrapper.record(AuditEvent(action="add", decision="allow"))

    assert wrapper.integrity_degraded is False


def test_degraded_latch_is_not_cleared_by_later_success() -> None:
    """
    闩是单向的：「已丢失证据」不能靠下一次写入成功抹掉（计划 §9.3），
    恢复只能靠进程重启或运维修复后重建 Runtime。
    """
    # 白盒故障/篡改注入或内部状态断言需要私有接缝，不扩展生产接口。
    # pylint: disable=protected-access
    logger = _StubLogger()
    wrapper = ProtectedAuditLogger(_FailingProvider(logger, RuntimeError("boom")), logger)
    with pytest.raises(AuditIntegrityError):
        wrapper.record(AuditEvent(action="add", decision="allow"))

    wrapper._provider = _StubProvider(logger)  # noqa: SLF001 - 模拟修复后重建
    wrapper.record(AuditEvent(action="add", decision="allow"))

    assert wrapper.integrity_degraded is True


def test_integrity_protected_marker_distinguishes_protected_from_plain() -> None:
    """
    ``integrity_protected`` 类标记是认证入口 fail-closed 的依据（R1）：
    受保护 logger 有标记，普通 AuditLogger 实现没有——按标记判，不靠异常类型。
    """
    logger = _StubLogger()
    assert ProtectedAuditLogger(_StubProvider(logger), logger).integrity_protected is True
    assert getattr(logger, "integrity_protected", False) is False
