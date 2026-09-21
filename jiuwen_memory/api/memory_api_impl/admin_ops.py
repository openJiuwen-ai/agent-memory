# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Jobs, admin policy, governance, audit verify, grant/revoke."""

from __future__ import annotations

import uuid
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
    PermissionContext,
)

from .local_support import (
    _ROOT,
    _evolve_space_action,
    _TrustedRequestTarget,
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
        # 来源、时效与凭据复核必须先于任务读取，再对任务真源 scope 判权。
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.READ, "job_status", scope or _ROOT, job_id)
        )
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
            security,
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
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.WRITE, "job_cancel", _ROOT, job_id)
        )
        info = self._scheduler.status(job_id)
        auth = self._authorize(
            security,
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
        auth = self._authorize(security, _ROOT, Action.ADMINISTER_SYSTEM, "admin_get", key)
        self._log(identity, "admin_get", key, target_scope=_ROOT, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        auth = self._authorize(security, _ROOT, Action.ADMINISTER_SYSTEM, "admin_set", key)
        self._log(identity, "admin_set", key, target_scope=_ROOT, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, security: RequestSecurityContext) -> dict[str, str]:
        identity = security.auth.actor
        auth = self._authorize(security, _ROOT, Action.ADMINISTER_SYSTEM, "admin_all")
        self._log(identity, "admin_all", target_scope=_ROOT, detail=auth)
        return self._policy.all()

    def inspect(
        self, unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(security, scope, Action.READ, "inspect")
        units = self._governance.inspect(unit_ids, scope)
        self._authorize_governance_units(units, security, "inspect")
        self._log(identity, "inspect", target_scope=scope, detail=auth)
        return units

    def trace(
        self, unit_id: str, scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        identity = security.auth.actor
        auth = self._authorize(security, scope, Action.READ, "trace", unit_id)
        units = self._governance.trace(unit_id, scope)
        self._authorize_governance_units(units, security, "trace")
        self._log(identity, "trace", unit_id, target_scope=scope, detail=auth)
        return units

    def _authorize_governance_units(
        self, units: list[MemoryUnit], security: RequestSecurityContext, entry: str
    ) -> None:
        # 只使用本次真源返回的条目，避免二次读取与最终返回内容的快照错位。
        for unit in units:
            context = PermissionContext(
                resource_type="memory_unit",
                unit_id=unit.id,
                scope=unit.scope,
                memory_type=str(unit.system_metadata.get("memory_type", "")).strip(),
                pipeline=str(unit.system_metadata.get("pipeline", "")).strip(),
                tags=tuple(unit.tags),
                metadata={key: str(value) for key, value in unit.system_metadata.items()},
            )
            self._authorize(security, unit.scope, Action.READ, entry, unit.id, context=context)

    def audit(
        self,
        filters: dict[str, str],
        *,
        security: RequestSecurityContext,
        limit: int = 100,
    ) -> list[AuditEvent]:
        identity = security.auth.actor
        # 审计查询跨 scope，按管理面角色闸门（根 scope READ_AUDIT）鉴权——授权记录
        # 按 action 精确匹配，不与普通 READ 互认。查询本身亦留痕。
        auth = self._authorize(security, _ROOT, Action.READ_AUDIT, "audit")
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
        auth = self._authorize(security, _ROOT, Action.VERIFY_AUDIT, "verify_audit")
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
        auth = self._authorize(security, grant.grantor, Action.SHARE, "grant")
        self._enforce_grant_ceiling(security, grant)
        stored = replace(grant, grant_id=uuid.uuid4().hex)
        self._log(
            identity,
            "grant",
            target_scope=grant.grantor,
            detail={**auth, "grant_id": stored.grant_id},
        )
        # 只写 Authorizer 的授权真源，不双写旧 PermissionManager：双写即第二真源，
        # revoke 只撤新 Store 时旧 Manager 仍放行（不变量 23 / 27）。
        for store in self._authorizer.management_grant_stores():
            store.add(stored)
        return stored

    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        identity = security.auth.actor
        self._require_trusted_context(
            security, _TrustedRequestTarget(Action.REVOKE_SHARE, "revoke", _ROOT)
        )
        if not grant.grant_id:
            raise ValidationError(
                "revoke requires a server-issued grant_id; conditional revocation is not supported"
            )
        pending = []
        for store in self._authorizer.management_grant_stores():
            lookup = getattr(store, "_get_for_revoke", None)
            revoke = getattr(store, "_revoke_bound", None)
            if not callable(lookup) or not callable(revoke):
                raise ValidationError("GrantStore lacks safe revocation binding capability")
            stored = lookup(grant.grant_id)
            if stored is None:
                continue  # 未知 ID 幂等，不根据请求 grantor 猜测真实归属。
            auth = self._authorize(security, stored.grantor, Action.REVOKE_SHARE, "revoke")
            pending.append((revoke, stored, auth))
        for revoke, stored, auth in pending:
            revoke(stored.grant_id, stored.grantor)
            self._log(
                identity,
                "revoke",
                target_scope=stored.grantor,
                detail={**auth, "grant_id": stored.grant_id},
            )
