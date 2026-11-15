# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆接口层（B 层，§9）：统一 Core API（形态无关）。

调用层（``jiuwen_memory_entry/``、``jiuwen_memory_adapter/``）只依赖本包：这里重导出
调用所需的全部类型，调用方无需 import 内核其他包。
"""

from jiuwen_memory.api.human_view import HumanView, HumanViewRuntime
from jiuwen_memory.common.errors import (
    AgentMemoryError,
    AuthenticationError,
    BackendError,
    ConflictError,
    NotFoundError,
    PartialFailureError,
    PermissionDeniedError,
    PolicyError,
    RateLimitedError,
    RelocationInProgressError,
    UnsupportedCapabilityError,
    ValidationError,
    WriteIncompleteError,
    safe_error_message,
)
from jiuwen_memory.common.log import (
    install_privacy_filter,
    metadata_for_log,
    redact_for_log,
    scope_for_log,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.capabilities import (
    CloseableSecurityCapability,
    share_security_lifecycle,
)
from jiuwen_memory.common.security.protection.local_media import LocalMediaImports
from jiuwen_memory.common.security.request_context import (
    get_request_id,
    new_request_context,
    reset_request_id,
    set_request_id,
)
from jiuwen_memory.common.security.space_roles import (
    SpaceContentRole,
    SpaceGovernanceRole,
)
from jiuwen_memory.common.security.types import (
    SECRET_PARAM_KEYS,
    Action,
    Credentials,
    Grant,
    RequestSecurityContext,
    Surface,
    reset_current,
    set_current,
    validate_actor_form,
)
from jiuwen_memory.common.type_def import (
    EXT_MAX_TOKENS,
    EXT_QUERY_CONTEXT,
    AuditEvent,
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    HierarchyKind,
    HierarchyRole,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    normalize,
)
from jiuwen_memory.common.type_def.continuity import (
    ContinuityEvent,
    ContinuityRecovery,
    ContinuityWrite,
    Duty,
    EvidenceRelation,
    MemoryRef,
    RelationRecord,
    RelationStatus,
    RoleViewQuery,
    TaskRequest,
    ViewBudget,
)
from jiuwen_memory.config.auto_overlay import (
    document_llm_instances,
    open_config_overlay,
    open_document_overlay,
    open_file_overlay,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeOptions
from jiuwen_memory.control import (
    BackgroundJobStartResult,
    BatchUpdateOutcome,
    BatchUpdateResult,
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
    UpdateSelector,
)
from jiuwen_memory.control.types import EvolveTaskOptions
from jiuwen_memory.retrieval import (
    DisclosureLevel,
    RetrievalResult,
    RetrievedItem,
    SchemaTemporalRetrievalResult,
    TrajectoryStep,
)

from .access_security import (
    build_configured_security_runtime,
    build_dev_authenticator,
    fixed_personal_owner,
    register_trusted_principal,
)
from .managed_configuration import ManagedConfiguration
from .memory_api import MemoryAPI
from .memory_api_impl import (
    AUTO_SECURITY_RUNTIME,
    MaintenancePorts,
    ManagedMemoryRuntime,
    ManagedServerRuntime,
    MemoryRuntime,
    ServerRuntime,
    assemble,
    assemble_maintenance_ports,
    assemble_managed_runtime,
    assemble_managed_server_runtime,
    assemble_runtime,
    assemble_server_runtime,
)
from .personal_memory import read_personal_record
from .runtime_configuration import sdk_continuity_runtime_identity
from .search_options import SearchOptions

__all__ = [
    "sdk_continuity_runtime_identity",
    "read_personal_record",
    "open_config_overlay",
    "open_document_overlay",
    "open_file_overlay",
    "document_llm_instances",
    "fixed_personal_owner",
    "ManagedConfiguration",
    "ManagedMemoryRuntime",
    "ManagedServerRuntime",
    "assemble_managed_runtime",
    "assemble_managed_server_runtime",
    "HumanView",
    "HumanViewRuntime",
    "SearchOptions",
    "EvolveTaskOptions",
    "HierarchyComposeOptions",
    "HierarchyKind",
    "HierarchyRole",
    "MemoryAPI",
    "assemble",
    "MaintenancePorts",
    "assemble_maintenance_ports",
    "assemble_runtime",
    "share_security_lifecycle",
    "CloseableSecurityCapability",
    "MemoryRuntime",
    "ServerRuntime",
    "assemble_server_runtime",
    "AUTO_SECURITY_RUNTIME",
    "build_dev_authenticator",
    "register_trusted_principal",
    "build_configured_security_runtime",
    # 数据模型（common.type_def）
    "Scope",
    "Context",
    "EXT_MAX_TOKENS",
    "EXT_QUERY_CONTEXT",
    "Modality",
    "MemoryTier",
    "LifecycleState",
    "MemoryUnit",
    "Segment",
    # 写入/修正/删除（control）
    "MemoryPatch",
    "MemoryListResult",
    "BackgroundJobStartResult",
    "BatchWriteItem",
    "BatchWriteOutcome",
    "BatchWriteResult",
    "BatchUpdateOutcome",
    "BatchUpdateResult",
    "UpdateSelector",
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
    "SpaceContentRole",
    "SpaceGovernanceRole",
    "SpaceUsage",
    "SpaceDeleteResult",
    # 检索（retrieval）
    "DisclosureLevel",
    "RetrievalResult",
    "SchemaTemporalRetrievalResult",
    "RetrievedItem",
    "TrajectoryStep",
    # 前置过滤（common.type_def）
    "FilterClause",
    "FilterExpr",
    "FilterOp",
    "normalize",
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
    "SECRET_PARAM_KEYS",
    "Surface",
    "new_request_context",
    "reset_request_id",
    "get_request_id",
    "set_request_id",
    "set_current",
    "reset_current",
    "validate_actor_form",
    # Access 错误映射（公开异常，transport 不识别内核内部模块）
    "AgentMemoryError",
    "AuthenticationError",
    "BackendError",
    "ConflictError",
    "NotFoundError",
    "PartialFailureError",
    "WriteIncompleteError",
    "PermissionDeniedError",
    "PolicyError",
    "RateLimitedError",
    "RelocationInProgressError",
    "UnsupportedCapabilityError",
    "ValidationError",
    "safe_error_message",
    # Access 本地视频导入预检（最终文件读取仍由内核执行）
    "LocalMediaImports",
    # 安全上下文构造（access 面需要，替代 legacy_request_context）
    "internal_context",
    # Access 日志隐私能力（adapter 不直接依赖内核 common 包）
    "install_privacy_filter",
    "metadata_for_log",
    "redact_for_log",
    "scope_for_log",
]


__all__ += [
    "ContinuityRecovery",
    "ContinuityWrite", "ContinuityEvent", "Duty", "EvidenceRelation", "MemoryRef",
    "RelationRecord", "RelationStatus", "RoleViewQuery", "TaskRequest", "ViewBudget",
]
