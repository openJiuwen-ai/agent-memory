# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LocalMemoryAPI facade: PEP + typed Control application ports.

Implementation helpers live in ``local_support.py`` and ``*_ops.py`` mixins.
Public class and method signatures are unchanged.
"""

from __future__ import annotations

from jiuwen_memory.api.memory_api import MemoryAPI
from jiuwen_memory.common.audit import AuditLogger
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.audit_integrity.base import (
    AuditIntegrityProvider,
    AuditVerificationLimits,
)
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuard
from jiuwen_memory.construction.router import EMPTY_ROUTE_TABLE, Router, RouteTable
from jiuwen_memory.control import collective
from jiuwen_memory.control.application import (
    GovernanceService,
    MemoryCommandService,
    MemoryQueryService,
    SpaceLifecycleService,
)
from jiuwen_memory.control.engine import MemoryEngine
from jiuwen_memory.control.governance import Governor
from jiuwen_memory.control.ingest_job import IngestJobController
from jiuwen_memory.control.membership import MembershipResolver
from jiuwen_memory.control.permission import PermissionManager
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.space import SpaceManager

from .admin_ops import AdminOpsMixin
from .local_support import (
    _first_family_predicate,
    _policy_int,
    _resolve_space_owner,
)
from .pep_ops import PepOpsMixin
from .query_ops import QueryOpsMixin
from .space_ops import SpaceOpsMixin
from .write_ops import WriteOpsMixin

__all__ = ["LocalMemoryAPI", "_first_family_predicate", "_resolve_space_owner"]


class LocalMemoryAPI(
    WriteOpsMixin,
    QueryOpsMixin,
    AdminOpsMixin,
    SpaceOpsMixin,
    PepOpsMixin,
    MemoryAPI,
):
    """单进程装配下的统一记忆接口实现（鉴权 + 审计 + 委派）。"""

    def __init__(
        self,
        engine: MemoryEngine,
        permission: PermissionManager,
        scheduler: Scheduler,
        policy: PolicyManager,
        governor: Governor,
        audit_logger: AuditLogger,
        space: SpaceManager,
        ingest_jobs: IngestJobController,
        audit_integrity_provider: AuditIntegrityProvider | None = None,
        audit_verify_guard: WorkloadGuard | None = None,
        audit_verify_limits: AuditVerificationLimits | None = None,
        membership: MembershipResolver | None = None,
        router: Router | None = None,
        commands: MemoryCommandService | None = None,
        queries: MemoryQueryService | None = None,
        space_lifecycle: SpaceLifecycleService | None = None,
        governance: GovernanceService | None = None,
    ) -> None:
        if audit_integrity_provider is not None and audit_verify_guard is None:
            raise ValidationError(
                "audit_integrity_provider requires a dedicated audit_verify_guard"
            )
        if audit_verify_limits is not None and not isinstance(
            audit_verify_limits, AuditVerificationLimits
        ):
            raise ValidationError("audit_verify_limits must be AuditVerificationLimits")
        self._engine = engine
        self._perm = permission
        self._scheduler = scheduler
        self._policy = policy
        self._governor = governor
        self._audit = audit_logger
        self._space = space
        self._ingest_jobs = ingest_jobs
        # 审计完整性 provider：未装配（None）时 verify_audit 返回 unsupported。装配后
        # 由本层 verify_audit 经 provider.verify 流式校验证明链。provider 持有的
        # ChainedAuditStore 与 self._audit 须是同一具名实例（装配侧保证）。
        self._audit_integrity = audit_integrity_provider
        # verify_audit 的全量验证是重操作，占专用 WorkloadGuard 的一个并发槽。
        # 未装配 provider 时 guard 可为空（verify 直接返回 unsupported）；provider 与
        # guard 必须成对注入，避免完整性验证无预算运行或与认证路径争抢同一预算。
        self._audit_verify_guard = audit_verify_guard
        # 可信服务端装配值；不从 verify_audit payload 或 provider 返回读取。
        self._audit_verify_limits = audit_verify_limits or AuditVerificationLimits()
        # 空间授权事实的读取算子。取可选参数是为了不改变既有装配的构造签名：不做
        # 空间级判定的部署无须提供它，此时判定实现的 requires_space_facts 也为假。
        self._membership = membership
        # 归属判定算子。未装配时判定表为空，写入侧 scope 必填、判定路径不可达，
        # 全链路行为与未启用该特性一致——这是可灰度上线的前提。
        self._router = router
        self._commands = commands if commands is not None else MemoryCommandService(engine)
        self._queries = queries if queries is not None else MemoryQueryService(engine)
        self._space_lifecycle = (
            space_lifecycle
            if space_lifecycle is not None
            else SpaceLifecycleService(engine, space)
        )
        self._governance = governance if governance is not None else GovernanceService(governor)

    @property
    def space_governance_enabled(self) -> bool:
        """本次装配是否启用空间治理，即判定实现是否读空间事实。

        供接入方与示例判断部署形态：启用后写入未注册空间由放行改为拒绝，调用方须先
        开通空间；未启用时 ``scope`` 的空间维可留空，行为与改造前一致。
        """
        return self._needs_space_facts()

    @property
    def route_table(self) -> RouteTable:
        """判定表的对外只读视图，供仓库内运维脚本（存量回填）取判定标签键集合。

        与内部使用的 :attr:`_route_table` 同源：运维脚本另读一次配置解析出的产物可能
        与运行时不一致，届时回填写入的标签键与判定实际使用的键集合会分叉。
        """
        return self._route_table

    @property
    def _route_table(self) -> RouteTable:
        """本次装配的判定表；未装配判定算子时为空表。

        取自判定算子实例而不另读一次配置：两条解析路径对同一份配置得出不同产物时，
        写入边界拒绝的键集合与判定实际写入的键集合会不一致，表现为判定自己写的标签
        在下一次写入时被自己的边界校验拒绝。
        """
        return self._router.table if self._router is not None else EMPTY_ROUTE_TABLE

    def _routing_enabled(self) -> bool:
        return not self._route_table.is_empty()

    def _space_fanout_limit(self) -> int:
        """一次调用参与的空间数上限，写入侧与检索侧同值（策略 ``space.fanout_limit``）。

        这是功能天花板而非性能参数：主体同时参与的空间超过该值时，超出部分写入不进候选、
        检索也取不到。够用与否取决于接入方的协作规模假设，因此可配置而不写死在内核里。

        两侧同值是有意的：写入侧按前 N 个候选落点，检索侧按前 N 个候选取数，取值分叉即
        出现「写得进去但检索不到」。
        """
        return _policy_int(
            self._policy, "space.fanout_limit", default=collective.SPACE_FANOUT_LIMIT
        )
