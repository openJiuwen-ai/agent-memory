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
    AuditIntegrityProvider,
    AuditVerificationResult,
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
