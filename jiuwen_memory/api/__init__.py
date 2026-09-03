# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆接口层（B 层，§9）：统一 Core API（形态无关）。

调用层（``jiuwen_memory_entry/``、``jiuwen_memory_adapter/``）只依赖本包：这里重导出
调用所需的全部类型，调用方无需 import 内核其他包。
"""

from jiuwen_memory.common.errors import (
    AgentMemoryError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PartialFailureError,
    PermissionDeniedError,
    PolicyError,
    RateLimitedError,
    ValidationError,
)
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import (
    Action,
    Credentials,
    Grant,
    RequestSecurityContext,
    Surface,
    reset_current,
    set_current,
)
from jiuwen_memory.common.type_def import (
    EXT_MAX_TOKENS,
    AuditEvent,
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control import (
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
    Channel,
    DeleteMode,
    DeleteSelector,
    IngestSubmission,
    JobInfo,
    JobStatus,
    MemoryListResult,
    MemoryPatch,
    PrincipalPath,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
    UpdateMode,
)
from jiuwen_memory.retrieval import (
    DisclosureLevel,
    RetrievalResult,
    RetrievedItem,
    TrajectoryStep,
)

from .memory_api import MemoryAPI
from .memory_api_impl import MemoryRuntime, assemble, assemble_runtime

__all__ = [
    "MemoryAPI",
    "assemble",
    "assemble_runtime",
    "MemoryRuntime",
    # 数据模型（common.type_def）
    "Scope",
    "Context",
    "EXT_MAX_TOKENS",
    "Modality",
    "MemoryTier",
    "LifecycleState",
    "MemoryUnit",
    "Segment",
    # 写入/修正/删除（control）
    "MemoryPatch",
    "MemoryListResult",
    "BatchWriteItem",
    "BatchWriteOutcome",
    "BatchWriteResult",
    "UpdateMode",
    "DeleteMode",
    "DeleteSelector",
    "PrincipalPath",
    "SpaceStatus",
    "SpacePolicy",
    "SpaceSpec",
    "SpaceInfo",
    "SpacePatch",
    "SpaceMember",
    "SpaceUsage",
    "SpaceDeleteResult",
    # 检索（retrieval）
    "DisclosureLevel",
    "RetrievalResult",
    "RetrievedItem",
    "TrajectoryStep",
    # 前置过滤（common.type_def）
    "FilterClause",
    "FilterExpr",
    "FilterOp",
    # 演进 + 任务调度（construction / control）
    "EvolveMode",
    "Channel",
    "IngestSubmission",
    "JobInfo",
    "JobStatus",
    # 治理 / 授权 / 请求安全上下文
    "AuditEvent",
    "Grant",
    "Action",
    "Credentials",
    "RequestSecurityContext",
    "Surface",
    "legacy_request_context",
    "new_request_context",
    "set_current",
    "reset_current",
    # Access 错误映射（公开异常，transport 不识别内核内部模块）
    "AgentMemoryError",
    "AuthenticationError",
    "ConflictError",
    "NotFoundError",
    "PartialFailureError",
    "PermissionDeniedError",
    "PolicyError",
    "RateLimitedError",
    "ValidationError",
]
