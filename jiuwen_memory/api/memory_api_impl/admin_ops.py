# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jobs, admin policy, governance, audit verify, grant/revoke."""

from __future__ import annotations

from dataclasses import replace

from jiuwen_memory.common.errors import (
    RateLimitedError,
    ValidationError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.audit_integrity.base import (
    DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
    DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
    AnchorState,
    AuditIntegrityStatus,
    AuditVerificationResult,
)
from jiuwen_memory.common.security.types import Action, Grant, RequestSecurityContext
from jiuwen_memory.common.type_def import (
    AuditEvent,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Scope,
)
from jiuwen_memory.control.ingest_job import INGEST_JOB_PREFIX, IngestSubmission
from jiuwen_memory.control.types import (
    Channel,
    JobInfo,
    JobStatus,
)

from .local_support import (
    _ROOT,
    _evolve_space_action,
    _validate_legacy_permission_actions,
)

logger = get_logger("jiuwen_memory.api.memory_api_impl.local_memory_api")


class AdminOpsMixin:
    """Jobs, admin policy, governance, audit verify, grant/revoke."""

    def submit_ingest(
        self,
        content: str,
        scope: Scope,
        source: Modality,
        *,
        security: RequestSecurityContext,
        payload_id: str,
        source_ref: str,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        system_metadata: dict[str, MetadataValueType] | None = None,
        user_metadata: dict[str, MetadataValueType] | None = None,
    ) -> IngestSubmission:
        self.check_write(
            scope,
            security,
            tags=tags,
            system_metadata=system_metadata,
            user_metadata=user_metadata,
        )
        return self._ingest_jobs.submit(
            payload_id=payload_id,
            source_ref=source_ref,
            scope=scope,
            task=lambda: self.add(
                content,
                scope,
                source,
                security=security,
                assets=assets,
                tags=tags,
                system_metadata=system_metadata,
                user_metadata=user_metadata,
            ),
        )

    def job_status(
        self,
        job_id: str,
        *,
        security: RequestSecurityContext,
        scope: Scope | None = None,
    ) -> JobInfo:
        identity = security.auth.actor
        # 先取任务（含其 scope），再据 identity 对该 scope 的 READ 权放行
        # （仅可查自身/已授权范围的任务）；status 为只读查询，先取后判权
        # 不产生副作用。
        if job_id.startswith(INGEST_JOB_PREFIX):
            if scope is None:
                raise ValidationError("ingest job status requires target scope")
            job = self._ingest_jobs.status(job_id, scope=scope)
            info = JobInfo(
                id=job.id,
                channel=Channel.BACKGROUND,
                mode="ingest",
                scope=job.scope,
                status=JobStatus(job.status),
                detail={
                    "payload_id": job.payload_id,
                    "source_ref": job.source_ref,
                    "unit_ids": ",".join(job.unit_ids),
                    "error": job.error,
                },
            )
        else:
            info = self._scheduler.status(job_id)
        auth = self._authorize(
            identity,
            info.scope,
            Action.READ,
            "job_status",
            job_id,
            space_action=_evolve_space_action(info.mode),
        )
        self._log(identity, "job_status", job_id, target_scope=info.scope, detail=auth)
        return info

    def job_cancel(self, job_id: str, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE
        # （与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        auth = self._authorize(
            identity,
            info.scope,
            Action.WRITE,
            "job_cancel",
            job_id,
            space_action=_evolve_space_action(info.mode),
        )
        self._log(identity, "job_cancel", job_id, target_scope=info.scope, detail=auth)
        self._scheduler.cancel(job_id)

    def admin_get(self, key: str, *, security: RequestSecurityContext) -> str:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_get", key)
        self._log(identity, "admin_get", key, target_scope=_ROOT, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.WRITE, "admin_set", key)
        self._log(identity, "admin_set", key, target_scope=_ROOT, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, security: RequestSecurityContext) -> dict[str, str]:
        identity = security.auth.actor
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_all")
        self._log(identity, "admin_all", target_scope=_ROOT, detail=auth)
        return self._policy.all()

    def inspect(
        self, unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(identity, scope, Action.READ, "inspect")
        self._log(identity, "inspect", target_scope=scope, detail=auth)
        return self._governance.inspect(unit_ids, scope)

    def trace(
        self, unit_id: str, scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(identity, scope, Action.READ, "trace", unit_id)
        self._log(identity, "trace", unit_id, target_scope=scope, detail=auth)
        return self._governance.trace(unit_id, scope)

    def audit(
        self,
        filters: dict[str, str],
        *,
        security: RequestSecurityContext,
        limit: int = 100,
    ) -> list[AuditEvent]:
        identity = security.auth.actor
        # 审计查询跨 scope，继续按既有管理面闸门（根 scope READ）鉴权；存量授权记录
        # 按 action 精确匹配，本接口 PR 不迁移其语义。READ_AUDIT 的切换须由独立的
        # 兼容性变更连同授权数据迁移一起完成。查询本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.READ, "audit")
        self._log(identity, "audit", target_scope=_ROOT, detail=auth)
        return self._governance.audit(filters, limit)

    def verify_audit(
        self,
        *,
        security: RequestSecurityContext,
        after_sequence: int = 0,
        page_size: int = DEFAULT_AUDIT_VERIFY_PAGE_SIZE,
        max_samples: int = DEFAULT_AUDIT_VERIFY_MAX_SAMPLES,
        anchor_policy: str = "if_configured",
    ) -> AuditVerificationResult:
        identity = security.auth.actor
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValidationError("after_sequence must be a non-negative integer")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            raise ValidationError("page_size must be a positive integer")
        if not isinstance(max_samples, int) or isinstance(max_samples, bool) or max_samples < 0:
            raise ValidationError("max_samples must be a non-negative integer")
        effective_page_size = min(page_size, self._audit_verify_limits.max_page_size)
        effective_max_samples = min(max_samples, self._audit_verify_limits.max_samples)
        if anchor_policy not in {"if_configured", "required", "skip"}:
            raise ValidationError(
                f"anchor_policy must be one of if_configured/required/skip, got {anchor_policy!r}"
            )
        # 验证审计链完整性：管理面根 scope 闸门使用独立 VERIFY_AUDIT 动作；
        # 验证本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.VERIFY_AUDIT, "verify_audit")
        if self._audit_integrity is None:
            # 未装配审计完整性 provider：诚实返回 unsupported，不抛错。
            self._log(identity, "verify_audit", target_scope=_ROOT, detail=auth)
            return AuditVerificationResult(
                status=AuditIntegrityStatus.UNSUPPORTED,
                checked_count=0,
                error_count=0,
                truncated=False,
                high_water_mark=0,
                key_epoch_range=(0, 0),
                anchor=AnchorState(checked=False),
                detail="audit integrity provider not configured",
            )
        # 全量验证在 WorkloadGuard 独立预算下执行：占用一个并发槽，耗尽则拒绝。
        guard = self._audit_verify_guard
        if guard is None:
            raise ValidationError("audit verify workload guard is not configured")
        if not guard.acquire():
            self._log(
                identity,
                "verify_audit",
                target_scope=_ROOT,
                # decision 表达授权结果；此处授权已通过，失败的是操作准入，不能让
                # decision=deny 的安全事件筛选把容量不足误报成权限拒绝。
                decision="allow",
                detail={
                    **auth,
                    "workload_guard": "exhausted",
                },
            )
            raise RateLimitedError("audit verification workload budget exhausted")
        # 与 audit() 一致，真正读取审计数据前先记录本次已授权且已准入的验证尝试。
        # provider 若因链篡改、schema 损坏等抛 AuditIntegrityError，此记录仍可追溯
        # 发起者与发生时间；异常继续原样传播，guard 仍由 finally 归还。
        self._log(identity, "verify_audit", target_scope=_ROOT, detail=auth)
        try:
            result = self._audit_integrity.verify(
                after_sequence=after_sequence,
                page_size=effective_page_size,
                max_samples=effective_max_samples,
                anchor_policy=anchor_policy,
            )
        finally:
            guard.release()
        # Provider 也受契约约束，但 PEP 对公网返回体再做一次 fail-safe 截断；自定义或
        # 旧 provider 即使错误地返回过多样本，也不能突破本次请求和可信装配的有效上限。
        if len(result.samples) > effective_max_samples:
            result = replace(
                result,
                samples=result.samples[:effective_max_samples],
                truncated=True,
            )
        return result

    def grant(self, grant: Grant, *, security: RequestSecurityContext) -> Grant:
        identity = security.auth.actor
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "grant")
        self._enforce_grant_ceiling(identity, grant)
        self._log(identity, "grant", target_scope=grant.grantor, detail=auth)
        # 旧 PermissionManager 尚不按 grant_id 定位，因此本期不生成 ID
        #（返回值原样回传，grant_id 保持入参值）。
        # 服务端生成 ID 与按 ID 定位随 GrantStore 实装一并落地。
        # PermissionManager 与安全域共用同一 Grant/Action 类型；管理动作在旧实现
        # 尚无角色闸门，必须先显式拒绝，不能借旧 ACL 语义放行。
        _validate_legacy_permission_actions(grant)
        self._perm.grant(grant)
        return grant

    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "revoke")
        self._log(identity, "revoke", target_scope=grant.grantor, detail=auth)
        # 旧 PermissionManager 按 grantor+grantee+action 条件撤销，不能按 grant_id
        # 定位。本期不据 grant_id 做任何判定，也不宣称精确撤销；
        # 契约要求的「按 ID 精确回收」随 GrantStore 实装落地。
        _validate_legacy_permission_actions(grant)
        self._perm.revoke(grant)
