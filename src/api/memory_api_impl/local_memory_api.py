""":class:`~api.memory_api.MemoryAPI` 的单进程实现（``LocalMemoryAPI``）+ 装配。

``LocalMemoryAPI`` 是鉴权与审计的执行点（PEP）：每个涉及租户数据/治理的
方法先构造权限上下文并调用 ``PermissionManager.check(..., context=...)``，
不通过抛 :class:`~common.errors.PermissionDeniedError`，通过后落入口审计并把
已鉴权的 target scope 透传到引擎/各控制算子（identity 不下沉）。同步方法以
``asyncio.run`` 桥接引擎的异步协程，供 CLI/脚本使用。各控制算子按其
抽象基类型注入。

:func:`build_kernel` / :func:`assemble` 把各层具体实现串成一个可直接
调用的内核——是「把整个项目串起来」的落点；生产装配只需在此换成真实实现。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from api.memory_api import MemoryAPI
from common.audit import AuditLogger
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import (
    EXT_MAX_TOKENS,
    AuditEvent,
    Context,
    FilterClause,
    FilterOp,
    MemoryUnit,
    Modality,
    Scope,
)
from construction import EvolveMode
from control.engine import MemoryEngine
from control.governance import Governor
from control.permission import PermissionManager
from control.policy import PolicyManager
from control.scheduler import Scheduler
from control.types import (
    Action,
    Channel,
    DeleteSelector,
    Grant,
    JobInfo,
    MemoryPatch,
    PermissionContext,
)
from retrieval.types import DisclosureLevel, RetrievalQuery, RetrievalResult

# 管理面（admin / 全局审计）没有具体 target scope，统一以「根 scope」为鉴权目标：
# 在真实 RBAC 后端下，「能对全局根 scope 行权」即等价于管理员闸门；在 allow_all
# 装配下为 no-op。租户数据/治理方法仍按各自的 target scope 鉴权。
_ROOT = Scope()


def _parse_max_tokens(raw: str | None) -> int | None:
    """解析 ``Context.extensions`` 的约定 key ``max_tokens`` 为披露预算（int）。

    缺失/空串 → ``None``（披露阶段用默认策略）；非整数 → :class:`~common.errors.ValidationError`
    （可预期的调用错误，与 ``RetrievalQuery`` 的 ``max_tokens<=0`` 校验同档）。
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"max_tokens must be an integer, got {raw!r}") from None


def _context_detail(context: PermissionContext | None) -> Dict[str, str]:
    if context is None:
        return {}
    detail = {
        "permission_resource_type": context.resource_type,
        "permission_memory_type": context.memory_type,
        "permission_pipeline": context.pipeline,
        "permission_unit_id": context.unit_id,
    }
    if context.tags:
        detail["permission_tags"] = ",".join(context.tags)
    return {key: value for key, value in detail.items() if value}


def _memory_type_from_filters(filters: List[FilterClause] | None) -> str:
    for clause in filters or []:
        if clause.field in {"memory_type", "metadata.memory_type"} and clause.op == FilterOp.EQ:
            return str(clause.value).strip()
    return ""


def _write_permission_context(
    scope: Scope,
    tags: List[str] | None,
    metadata: Dict[str, str] | None,
) -> PermissionContext:
    meta = {key: str(value) for key, value in (metadata or {}).items()}
    return PermissionContext(
        resource_type="write_input",
        memory_type=meta.get("memory_type", "").strip(),
        pipeline=meta.get("pipeline", "").strip(),
        scope=scope,
        tags=tuple(tags or ()),
        metadata=meta,
    )


def _recall_permission_context(
    context: Context,
    filters: List[FilterClause] | None,
) -> PermissionContext:
    extensions = {key: str(value) for key, value in context.extensions.items()}
    return PermissionContext(
        resource_type="query",
        memory_type=extensions.get("memory_type", "").strip() or _memory_type_from_filters(filters),
        pipeline=extensions.get("pipeline", "").strip(),
        scope=context.scope,
        metadata=extensions,
    )


def _selector_permission_context(selector: DeleteSelector, scope: Scope) -> PermissionContext:
    return PermissionContext(
        resource_type="delete_selector",
        scope=scope,
        tags=tuple(selector.tags),
        metadata={"mode": selector.mode.value},
    )


def _unit_lookup_permission_context(unit_id: str, scope: Scope) -> PermissionContext:
    return PermissionContext(
        resource_type="memory_unit_lookup",
        unit_id=unit_id,
        scope=scope,
    )


class LocalMemoryAPI(MemoryAPI):
    """单进程装配下的统一记忆接口实现（鉴权 + 审计 + 委派）。"""

    def __init__(
        self,
        engine: MemoryEngine,
        permission: PermissionManager,
        scheduler: Scheduler,
        policy: PolicyManager,
        governor: Governor,
        audit_logger: AuditLogger,
    ) -> None:
        self._engine = engine
        self._perm = permission
        self._scheduler = scheduler
        self._policy = policy
        self._governor = governor
        self._audit = audit_logger

    # -- 鉴权 + 审计公共点 --------------------------------------------------- #

    def _record_audit(
        self,
        identity: Scope,
        action: str,
        *,
        target_id: str = "",
        decision: str = "allow",
        detail: Dict[str, str] | None = None,
    ) -> None:
        payload = dict(detail or {})
        payload.setdefault("decision", decision)
        self._audit.record(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor=identity,
                action=action,
                target_id=target_id,
                layer="api",
                decision=decision,
                occurred_at=datetime.now(timezone.utc),
                detail=payload,
            )
        )

    def _authorize(
        self,
        identity: Scope,
        target: Scope,
        action: Action,
        audit_action: str,
        target_id: str = "",
        *,
        context: PermissionContext | None = None,
        check_permission: bool = True,
    ) -> Dict[str, str]:
        if not check_permission:
            return {
                "permission_check": "disabled",
                "permission_reason": "permission check disabled",
                **_context_detail(context),
            }
        if not self._perm.check(identity, target, action, context=context):
            self._record_audit(
                identity,
                audit_action,
                target_id=target_id,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": f"permission denied for action={action.value}",
                    **_context_detail(context),
                },
            )
            raise PermissionDeniedError(action.value)
        return {
            "permission_check": "enabled",
            "permission_reason": "permission check passed",
            **_context_detail(context),
        }

    def _log(
        self,
        identity: Scope,
        action: str,
        target_id: str = "",
        *,
        decision: str = "allow",
        detail: Dict[str, str] | None = None,
    ) -> None:
        self._record_audit(
            identity,
            action,
            target_id=target_id,
            decision=decision,
            detail=detail,
        )

    # -- 数据面 ------------------------------------------------------------- #

    def write(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        assets: List[str] | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> List[MemoryUnit]:
        return asyncio.run(
            self.write_async(
                content,
                scope,
                source,
                identity=identity,
                assets=assets,
                tags=tags,
                metadata=metadata,
                occurred_at=occurred_at,
            )
        )

    async def write_async(
        self,
        content: str,
        scope: Scope,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        assets: List[str] | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> List[MemoryUnit]:
        permission_context = _write_permission_context(scope, tags, metadata)
        auth = self._authorize(
            identity,
            scope,
            Action.WRITE,
            "write",
            context=permission_context,
        )
        units = await self._engine.write(
            content,
            scope,
            source,
            assets=assets,
            tags=tags,
            metadata=metadata,
            occurred_at=occurred_at,
        )
        self._log(identity, "write", detail=auth)
        return units

    def recall(
        self,
        query: str,
        context: Context,
        *,
        identity: Scope,
        filters: List[FilterClause] | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        # Context 在边界处拆包：scope 照旧作独立轴下推（鉴权 + 检索），extensions 写入
        # 调用级 options 顺 parser 透传给自定义检索模块；Context 对象本身不进内核。
        # 约定 key max_tokens（自适应披露预算）在此解析为 typed int 写入 RetrievalQuery，
        # 并从透传 extensions 中移除，避免与内核已解释的字段重复。
        permission_context = _recall_permission_context(context, filters)
        auth = self._authorize(
            identity,
            context.scope,
            Action.READ,
            "recall",
            context=permission_context,
        )
        options = dict(context.extensions)
        max_tokens = _parse_max_tokens(options.pop(EXT_MAX_TOKENS, None))
        rq = RetrievalQuery(
            text=query,
            filters=list(filters or []),
            as_of=as_of,
            top_k=top_k,
            disclosure=disclosure,
            max_tokens=max_tokens,
            with_trajectory=with_trajectory,
            extensions=options,
        )
        result = asyncio.run(self._engine.recall(context.scope, rq))
        self._log(identity, "recall", detail=auth)
        return result

    def get(
        self, unit_id: str, scope: Scope, *, identity: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        self._authorize(
            identity,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        auth = self._authorize(
            identity,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=permission_context,
        )
        unit = asyncio.run(self._engine.get(unit_id, scope, as_of))
        self._log(identity, "get", unit_id, detail={**auth, "after_unit_id": unit.id})
        return unit

    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, identity: Scope
    ) -> MemoryUnit:
        self._authorize(
            identity,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        auth = self._authorize(
            identity,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=permission_context,
        )
        before = asyncio.run(self._engine.get(unit_id, scope, None))
        unit = asyncio.run(self._engine.update(unit_id, scope, patch))
        self._log(
            identity,
            "update",
            unit_id,
            detail={**auth, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    def delete(self, selector: DeleteSelector, *, identity: Scope) -> List[str]:
        if not selector.unit_ids and not selector.tags and selector.before is None:
            raise ValidationError("DeleteSelector requires unit_ids, tags, or before")
        # 按 selector 的目标 scope 鉴权 DELETE；未限定 scope（如纯按 id/标签的
        # 跨范围删除）则退到根 scope 闸门，要求更高权限。
        target = selector.scope or _ROOT
        selector_context = _selector_permission_context(selector, target)
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        contexts = asyncio.run(self._engine.permission_contexts_for_delete(selector))
        if not contexts:
            auth = self._authorize(
                identity,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    identity,
                    permission_context.scope,
                    Action.DELETE,
                    "delete",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        deleted = asyncio.run(self._engine.delete(selector))
        self._log(
            identity,
            "delete",
            detail={**auth, "before_unit_ids": json.dumps(deleted, ensure_ascii=False)},
        )
        return deleted

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        identity: Scope,
    ) -> str:
        auth = self._authorize(identity, scope, Action.WRITE, "evolve")
        job_id = asyncio.run(self._engine.evolve(scope, mode, channel))
        self._log(identity, "evolve", detail={**auth, "job_id": job_id})
        return job_id

    # -- 任务调度（直达 Scheduler） ----------------------------------------- #

    def job_status(self, job_id: str, *, identity: Scope) -> JobInfo:
        # 先取任务（含其 scope），再据 identity 对该 scope 的 READ 权放行（仅可查
        # 自身/已授权范围的任务）；status 为只读查询，先取后判权不产生副作用。
        info = self._scheduler.status(job_id)
        auth = self._authorize(identity, info.scope, Action.READ, "job_status", job_id)
        self._log(identity, "job_status", job_id, detail=auth)
        return info

    def job_cancel(self, job_id: str, *, identity: Scope) -> None:
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE（与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        auth = self._authorize(identity, info.scope, Action.WRITE, "job_cancel", job_id)
        self._log(identity, "job_cancel", job_id, detail=auth)
        self._scheduler.cancel(job_id)

    # -- admin（直达 PolicyManager；管理面闸门 = 根 scope 鉴权） ------------- #

    def admin_get(self, key: str, *, identity: Scope) -> str:
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_get", key)
        self._log(identity, "admin_get", key, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, identity: Scope) -> None:
        auth = self._authorize(identity, _ROOT, Action.WRITE, "admin_set", key)
        self._log(identity, "admin_set", key, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, identity: Scope) -> Dict[str, str]:
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_all")
        self._log(identity, "admin_all", detail=auth)
        return self._policy.all()

    # -- 治理（直达 Governor） ---------------------------------------------- #

    def inspect(
        self, unit_ids: List[str], scope: Scope, *, identity: Scope
    ) -> List[MemoryUnit]:
        auth = self._authorize(identity, scope, Action.READ, "inspect")
        self._log(identity, "inspect", detail=auth)
        return self._governor.inspect(unit_ids)

    def trace(self, unit_id: str, scope: Scope, *, identity: Scope) -> List[MemoryUnit]:
        auth = self._authorize(identity, scope, Action.READ, "trace", unit_id)
        self._log(identity, "trace", unit_id, detail=auth)
        return self._governor.trace(unit_id)

    def audit(
        self, filters: Dict[str, str], *, identity: Scope, limit: int = 100
    ) -> List[AuditEvent]:
        # 审计查询跨 scope，按管理面闸门（根 scope READ）鉴权；查询本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.READ, "audit")
        self._log(identity, "audit", detail=auth)
        return self._governor.audit(filters, limit)

    # -- 跨 scope 授权（直达 PermissionManager） ---------------------------- #

    def grant(self, grant: Grant, *, identity: Scope) -> None:
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "grant")
        self._log(identity, "grant", detail=auth)
        self._perm.grant(grant)

    def revoke(self, grant: Grant, *, identity: Scope) -> None:
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "revoke")
        self._log(identity, "revoke", detail=auth)
        self._perm.revoke(grant)
