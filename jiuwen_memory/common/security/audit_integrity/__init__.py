# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""审计完整性能力（F05 §Audit Integrity）。

与 :mod:`jiuwen_memory.common.audit` 的职责分工：

- :class:`~jiuwen_memory.common.audit.base.AuditLogger` 只做普通事件记录/查询（``record`` /
  ``query``），**不**把 HMAC、链头、验证方法塞回基类--默认空实现会把「不支持」
  伪装成「支持但较弱」，形成 fail-open 公共契约。
- 本包提供**可插拔的完整性能力**：版本化规范化、链式 HMAC 证明、原子链式追加、
  有界流式验证与可选外部锚点。具体审计后端可同时实现
  :class:`~jiuwen_memory.common.security.audit_integrity.chain_store.ChainedAuditStore`，
  但这是**显式 capability**，不从 target 名推断。

模块结构::

    audit_integrity/
    ├── base.py              # Provider/Producer、proof/result/status、错误
    └── chain_store.py       # ChainStore capability、链头、锚点契约

``audit_integrity_impl``（版本化规范化 + 链式 HMAC 实现等）随实装 PR 合入。
"""

from .base import (
    DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    HARD_MAX_AUDIT_VERIFY_PAGE_SIZE,
    HARD_MAX_AUDIT_VERIFY_SAMPLES,
    AnchorState,
    AnchorStatus,
    AuditIntegrityError,
    AuditIntegrityProducer,
    AuditIntegrityProvider,
    AuditIntegrityStatus,
    AuditMigrationRequiredError,
    AuditSchemaError,
    AuditVerificationLimits,
    AuditVerificationResult,
    ChainConflictError,
    KeyCapabilityError,
    Proof,
)
from .chain_store import (
    GENESIS_DIGEST,
    AnchorRecord,
    AuditAnchor,
    ChainedAuditStore,
    ChainedRecord,
    ChainHead,
    ChainSnapshot,
    ChainStoreCapability,
)

__all__ = [
    "AnchorRecord",
    "AnchorState",
    "AnchorStatus",
    "AuditAnchor",
    "AuditIntegrityError",
    "AuditIntegrityProducer",
    "AuditIntegrityProvider",
    "AuditIntegrityStatus",
    "AuditMigrationRequiredError",
    "AuditSchemaError",
    "AuditVerificationLimits",
    "AuditVerificationResult",
    "ChainConflictError",
    "ChainHead",
    "ChainSnapshot",
    "ChainStoreCapability",
    "ChainedAuditStore",
    "ChainedRecord",
    "DEFAULT_AUDIT_VERIFY_MAX_SAMPLES",
    "DEFAULT_AUDIT_VERIFY_PAGE_SIZE",
    "GENESIS_DIGEST",
    "HARD_MAX_AUDIT_VERIFY_PAGE_SIZE",
    "HARD_MAX_AUDIT_VERIFY_SAMPLES",
    "KeyCapabilityError",
    "Proof",
]
