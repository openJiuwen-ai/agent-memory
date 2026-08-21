"""控制层（C 层，记忆管理面）算子基类。

控制层是管理面（架构 §2）：不直接生产/检索记忆，而是管理它们的
生命周期与使用规则，职责贯穿 §3.1/§8（生命周期）、§3.2（scope 权限）、
§12（治理/审计）、§13（可变策略）。各职责拆成可插拔算子：

- :class:`~control.engine.MemoryEngine` 记忆引擎（接口层 §9 各语义的编排中枢）
- :class:`~control.lifecycle.LifecycleManager` 生命周期（状态流转/到期清理）
- :class:`~control.governance.Governor` 治理（检视/血缘回溯/审计查询）
- :class:`~control.permission.PermissionManager` 权限（跨 scope 授权与校验）
- :class:`~control.scheduler.Scheduler` 演进调度（hot/background 双通道）
- :class:`~control.policy.PolicyManager` 运行时可变策略（§13.4 admin 落点）
- :class:`~control.pipeline.MemoryPipeline` 记忆类型 pipeline 路由（跨构建/查询 profile 编排）
- :class:`~control.space.SpaceManager` space 生命周期、策略、成员与 offboarding 管理

控制层驱动构建层做演进、经 ``src/storage`` 读写状态；审计记录走
``src/common`` 的 :class:`~common.audit.AuditLogger`（横切共用）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class ControlOperatorType(str, Enum):
    ENGINE = "engine"
    PIPELINE = "pipeline"
    LIFECYCLE = "lifecycle"
    GOVERNOR = "governor"
    PERMISSION = "permission"
    SCHEDULER = "scheduler"
    INGEST_JOB = "ingest_job"
    POLICY = "policy"
    SPACE = "space"


class ControlOperator(ABC):
    """所有控制层算子的自描述契约。"""

    @abstractmethod
    def operator_type(self) -> ControlOperatorType:
        """返回本算子的类型。"""

    @abstractmethod
    def health(self) -> None:
        """存活探测：健康时返回 ``None``，否则抛出异常。"""
