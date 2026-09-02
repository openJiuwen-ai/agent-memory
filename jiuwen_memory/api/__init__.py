# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆接口层（B 层，§9）：统一 Core API（形态无关）。

调用层（``jiuwen_memory_entry/``、``jiuwen_memory_adapter/``）只依赖本包：这里重导出
调用所需的全部类型，调用方无需 import 内核其他包。
"""

from jiuwen_memory.common.security.types import Action, Grant
from jiuwen_memory.common.type_def import (
    AuditEvent,
    Context,
    FilterClause,
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
    Channel,
    DeleteMode,
    DeleteSelector,
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
from .memory_api_impl import Kernel, LocalMemoryAPI, assemble, build_kernel

__all__ = [
    "MemoryAPI",
    # 单进程实现 + 装配（参考装配，把各层具体实现串起来）
    "LocalMemoryAPI",
    "Kernel",
    "assemble",
    "build_kernel",
    # 数据模型（common.type_def）
    "Scope",
    "Context",
    "Modality",
    "MemoryTier",
    "LifecycleState",
    "MemoryUnit",
    "Segment",
    # 写入/修正/删除（control）
    "MemoryPatch",
    "MemoryListResult",
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
    "FilterOp",
    # 演进 + 任务调度（construction / control）
    "EvolveMode",
    "Channel",
    "JobInfo",
    "JobStatus",
    # 治理 / 授权（common.type_def / common.security.types）
    "AuditEvent",
    # 授权管理走安全域 Grant/Action（grant_id 精确撤销契约）；旧 control 域
    # 同名类型不再从本包导出，避免新旧授权域双公共入口
    "Grant",
    "Action",
]
