"""记忆接口层（B 层，§9）：统一 Core API（形态无关）。

调用层（``bootstrap/``、``agent_plugin/``）只依赖本包：这里重导出
调用所需的全部类型，调用方无需 import 内核其他包。
"""

from common.type_def import (
    AuditEvent,
    FilterClause,
    FilterOp,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
)
from construction import EvolveMode
from control import (
    Action,
    Channel,
    DeleteMode,
    DeleteSelector,
    Grant,
    JobInfo,
    JobStatus,
    MemoryPatch,
    UpdateMode,
)
from retrieval import (
    DisclosureLevel,
    RetrievalResult,
    RetrievedItem,
    TrajectoryStep,
)

from .memory_api import MemoryAPI

__all__ = [
    "MemoryAPI",
    # 数据模型（common.type_def）
    "Scope",
    "Modality",
    "MemoryTier",
    "LifecycleState",
    "MemoryUnit",
    # 写入/修正/删除（control）
    "MemoryPatch",
    "UpdateMode",
    "DeleteMode",
    "DeleteSelector",
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
    # 治理 / 授权（control / common.type_def）
    "AuditEvent",
    "Grant",
    "Action",
]
