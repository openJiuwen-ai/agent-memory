# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ProtectedAuditLogger - 把普通 AuditLogger 升级为链式完整性审计（F05 §Audit Integrity）。

启用审计完整性期时，PEP（LocalMemoryAPI）与 surface 记录入口事件都不再直连
:class:`~jiuwen_memory.common.audit.base.AuditLogger`，而是经本 wrapper：

- ``record(event)`` -> :meth:`AuditIntegrityProvider.record_chained`：规范化、计算 proof、
  原子追加进链。关键事件写入失败时 provider 抛
  :class:`~jiuwen_memory.common.security.audit_integrity.base.AuditIntegrityError`，由调用方
  按事件等级 fail-closed--wrapper 不吞错、不降级为无完整性审计。
- ``query(filters, limit)`` -> 底层 AuditLogger.query：链式后端的 query 从链读取
  （脱去 proof），故查询经此透传即可返回事件。

wrapper 不持有需自管 ``close`` 的资源：构造时通过 ``provider.chain_store()`` 对象 identity
校验其与 AuditLogger 是同一实例，而不是只靠具名配置或注释约定；资源仍由审计日志的
生命周期所有者统一关闭。

**接口先行说明**：本 wrapper 属契约层（纯委托，无密码学）；装配它需要
``AuditIntegrityProvider`` 实例，随实装 PR 出现。本期无调用点，仅固定接口。
"""

from __future__ import annotations

from jiuwen_memory.common.audit.base import AuditLogger
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import AuditIntegrityProvider
from jiuwen_memory.common.type_def import AuditEvent


class ProtectedAuditLogger(AuditLogger):
    """把 record 转为链式完整性写入、query 透传底层后端的 AuditLogger。"""

    def __init__(self, provider: AuditIntegrityProvider, audit_logger: AuditLogger) -> None:
        if provider.chain_store() is not audit_logger:
            raise ValidationError(
                "audit integrity provider and AuditLogger must use the same store instance"
            )
        self._provider = provider
        self._audit = audit_logger

    def record(self, event: AuditEvent) -> None:
        """经 provider 记链；写入失败抛 :class:`AuditIntegrityError`（fail-closed）。"""
        self._provider.record_chained(event)

    def query(self, filters: dict[str, str], limit: int = 100) -> list[AuditEvent]:
        """查询透传底层后端（链式模式下读 chain 记录，脱 proof）。"""
        return self._audit.query(filters, limit)
