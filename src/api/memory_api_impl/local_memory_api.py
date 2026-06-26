""":class:`~api.memory_api.MemoryAPI` 的单进程实现（``LocalMemoryAPI``）+ 装配。

``LocalMemoryAPI`` 是鉴权与审计的执行点（PEP）：每个涉及租户数据/治理的
方法先 ``PermissionManager.check(identity, target, action)``，不通过抛
:class:`~common.errors.PermissionDeniedError`，通过后落入口审计并把已鉴权的
target scope 透传到引擎/各控制算子（identity 不下沉）。同步方法以 ``asyncio.run``
桥接引擎的异步协程，供 CLI/脚本使用。各控制算子按其抽象基类型注入。

:func:`build_kernel` / :func:`assemble` 把各层具体实现串成一个可直接
调用的内核——是「把整个项目串起来」的落点；生产装配只需在此换成真实实现。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from common.audit import AuditLogger
from common.errors import PermissionDeniedError, ValidationError
from common.type_def import (
    EXT_MAX_TOKENS,
    AuditEvent,
    Context,
    FilterClause,
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
from control.types import Action, Channel, DeleteSelector, Grant, JobInfo, MemoryPatch
from retrieval.types import DisclosureLevel, RetrievalQuery, RetrievalResult

from api.memory_api import MemoryAPI

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

    def _authorize(self, identity: Scope, target: Scope, action: Action) -> None:
        if not self._perm.check(identity, target, action):
            raise PermissionDeniedError(action.value)

    def _log(self, identity: Scope, action: str, target_id: str = "") -> None:
        self._audit.record(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor=identity,
                action=action,
                target_id=target_id,
                layer="api",
                occurred_at=datetime.now(timezone.utc),
            )
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
        self._authorize(identity, scope, Action.WRITE)
        self._log(identity, "write")
        return await self._engine.write(
            content,
            scope,
            source,
            assets=assets,
            tags=tags,
            metadata=metadata,
            occurred_at=occurred_at,
        )

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
        self._authorize(identity, context.scope, Action.READ)
        self._log(identity, "recall")
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
        return asyncio.run(self._engine.recall(context.scope, rq))

    def get(
        self, unit_id: str, scope: Scope, *, identity: Scope, as_of: datetime | None = None
    ) -> MemoryUnit:
        self._authorize(identity, scope, Action.READ)
        self._log(identity, "get", unit_id)
        return asyncio.run(self._engine.get(unit_id, scope, as_of))

    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, identity: Scope
    ) -> MemoryUnit:
        self._authorize(identity, scope, Action.UPDATE)
        self._log(identity, "update", unit_id)
        return asyncio.run(self._engine.update(unit_id, scope, patch))

    def delete(self, selector: DeleteSelector, *, identity: Scope) -> List[str]:
        # 按 selector 的目标 scope 鉴权 DELETE；未限定 scope（如纯按 id/标签的
        # 跨范围删除）则退到根 scope 闸门，要求更高权限。
        self._authorize(identity, selector.scope or _ROOT, Action.DELETE)
        self._log(identity, "delete")
        return asyncio.run(self._engine.delete(selector))

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        identity: Scope,
    ) -> str:
        self._authorize(identity, scope, Action.WRITE)
        self._log(identity, "evolve")
        return asyncio.run(self._engine.evolve(scope, mode, channel))

    # -- 任务调度（直达 Scheduler） ----------------------------------------- #

    def job_status(self, job_id: str, *, identity: Scope) -> JobInfo:
        # 先取任务（含其 scope），再据 identity 对该 scope 的 READ 权放行（仅可查
        # 自身/已授权范围的任务）；status 为只读查询，先取后判权不产生副作用。
        info = self._scheduler.status(job_id)
        self._authorize(identity, info.scope, Action.READ)
        self._log(identity, "job_status", job_id)
        return info

    def job_cancel(self, job_id: str, *, identity: Scope) -> None:
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE（与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        self._authorize(identity, info.scope, Action.WRITE)
        self._log(identity, "job_cancel", job_id)
        self._scheduler.cancel(job_id)

    # -- admin（直达 PolicyManager；管理面闸门 = 根 scope 鉴权） ------------- #

    def admin_get(self, key: str, *, identity: Scope) -> str:
        self._authorize(identity, _ROOT, Action.READ)
        self._log(identity, "admin_get", key)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, identity: Scope) -> None:
        self._authorize(identity, _ROOT, Action.WRITE)
        self._log(identity, "admin_set", key)
        self._policy.set(key, value)

    def admin_all(self, *, identity: Scope) -> Dict[str, str]:
        self._authorize(identity, _ROOT, Action.READ)
        self._log(identity, "admin_all")
        return self._policy.all()

    # -- 治理（直达 Governor） ---------------------------------------------- #

    def inspect(
        self, unit_ids: List[str], scope: Scope, *, identity: Scope
    ) -> List[MemoryUnit]:
        self._authorize(identity, scope, Action.READ)
        self._log(identity, "inspect")
        return self._governor.inspect(unit_ids)

    def trace(self, unit_id: str, scope: Scope, *, identity: Scope) -> List[MemoryUnit]:
        self._authorize(identity, scope, Action.READ)
        self._log(identity, "trace", unit_id)
        return self._governor.trace(unit_id)

    def audit(
        self, filters: Dict[str, str], *, identity: Scope, limit: int = 100
    ) -> List[AuditEvent]:
        # 审计查询跨 scope，按管理面闸门（根 scope READ）鉴权；查询本身亦留痕。
        self._authorize(identity, _ROOT, Action.READ)
        self._log(identity, "audit")
        return self._governor.audit(filters, limit)

    # -- 跨 scope 授权（直达 PermissionManager） ---------------------------- #

    def grant(self, grant: Grant, *, identity: Scope) -> None:
        self._authorize(identity, grant.grantor, Action.SHARE)
        self._log(identity, "grant")
        self._perm.grant(grant)

    def revoke(self, grant: Grant, *, identity: Scope) -> None:
        self._authorize(identity, grant.grantor, Action.SHARE)
        self._log(identity, "revoke")
        self._perm.revoke(grant)
