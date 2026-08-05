""":class:`~api.memory_api.MemoryAPI` 的单进程实现（``LocalMemoryAPI``）+ 装配。

``LocalMemoryAPI`` 是鉴权与审计的执行点（PEP），且是**唯一的业务 PEP**：每个
涉及租户数据/治理的方法把 verb 映射为封闭的
:class:`~common.security.types.Action`、从真源构造
:class:`~common.security.types.ResourceDescriptor`、从
:class:`~common.security.types.RequestSecurityContext` 派生
:class:`~common.security.types.AuthorizationEnvironment`，再调用
:class:`~common.security.authorization.Authorizer`（PDP）。不通过抛
:class:`~common.errors.PermissionDeniedError`，通过后落带 actor 与稳定决策标识
（``rule`` / :class:`~common.security.types.DenyReason`）的入口审计，并把已鉴权的
target scope 透传到引擎/各控制算子（调用方身份不下沉）。同步方法以
``asyncio.run`` 桥接引擎的异步协程，供 CLI/脚本使用。各控制算子按其
抽象基类型注入。

安全输入只有 ``security`` 一个：actor 取自 ``security.auth.actor``，不从
ContextVar 取、也不从业务 payload 取（F05 §显式上下文优于环境权限）。

:func:`build_kernel` / :func:`assemble` 把各层具体实现串成一个可直接
调用的内核——是「把整个项目串起来」的落点；生产装配只需在此换成
真实实现。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from api.memory_api import MemoryAPI
from common.audit import AuditLogger
from common.errors import NotFoundError, PermissionDeniedError, PolicyError, ValidationError
from common.security.authorization import Authorizer
from common.security.types import (
    Action,
    AuthorizationEnvironment,
    RequestSecurityContext,
    ResourceDescriptor,
)
from common.type_def import (
    EXT_MAX_TOKENS,
    RESERVED_METADATA_KEYS,
    AuditEvent,
    Context,
    FilterClause,
    FilterExpr,
    FilterOp,
    MemoryUnit,
    Modality,
    Scope,
    and_merge,
    canonical_filter_field,
    extract_required_equality,
    filter_field_metadata_key,
    iter_clauses,
    normalize,
)
from construction import EvolveMode
from control.engine import MemoryEngine
from control.governance import Governor
from control.permission import PermissionManager
from control.policy import PolicyManager
from control.scheduler import Scheduler
from control.space import SpaceManager
from control.types import (
    Channel,
    DeleteMode,
    DeleteSelector,
    Grant,
    JobInfo,
    MemoryListResult,
    MemoryPatch,
    PermissionContext,
    SpaceDeleteResult,
    SpaceInfo,
    SpaceMember,
    SpacePatch,
    SpacePolicy,
    SpaceSpec,
    SpaceStatus,
    SpaceUsage,
)
from retrieval.types import DisclosureLevel, RetrievalQuery, RetrievalResult

# 管理面（admin / 全局审计）没有具体 target scope，统一以「根 scope」
# 为鉴权目标：在真实 RBAC 后端下，「能对全局根 scope 行权」即等价于
# 管理员闸门；在 allow_all
# 装配下为 no-op。租户数据/治理方法仍按各自的 target scope 鉴权。
_ROOT = Scope()


def _parse_max_tokens(raw: str | None) -> int | None:
    """解析 ``Context.extensions`` 的约定 key ``max_tokens`` 为披露预算（int）。

    缺失/空串 → ``None``（披露阶段用默认策略）；非整数 →
    :class:`~common.errors.ValidationError`
    （可预期的调用错误，与 ``RetrievalQuery`` 的 ``max_tokens<=0`` 校验同档）。
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"max_tokens must be an integer, got {raw!r}") from None


def _context_detail(context: PermissionContext | None) -> dict[str, str]:
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
    if context.scope.space:
        detail["permission_space"] = context.scope.space
    return {key: value for key, value in detail.items() if value}


def _required_filter_metadata(filters: FilterExpr | None) -> dict[str, Any]:
    """提取 FilterExpr 逻辑上强制的唯一等值，作为查询路由的 filter 侧候选值。

    ``_recall_permission_context`` 随后按 S03「MemoryPipeline 路由规则」让
    ``Context.extensions`` 中的非空值覆盖本结果，使权限路由与 Pipeline 执行路由同源。
    OR 多值、NOT、AND 冲突和未限定字段不会从 filters 产出候选值；若 extensions 也未
    提供，则权限路由进入最小权限 ``fallback``。
    """
    routed: dict[str, str] = {}
    ambiguous: set[str] = set()
    fields = {clause.field for clause in iter_clauses(filters)}
    for field in fields:
        value = extract_required_equality(filters, field)
        if value is None:
            continue
        normalized = str(value).strip()
        key = filter_field_metadata_key(field)
        if not normalized or key in ambiguous:
            continue
        if key in routed and routed[key] != normalized:
            routed.pop(key)
            ambiguous.add(key)
            continue
        routed[key] = normalized
    return routed


def _reject_reserved_metadata(metadata: dict[str, Any] | None) -> None:
    """写入/更新边界拒绝系统保留 key。

    索引投影会用真源系统字段覆盖同名用户 metadata，而 UnitReader 复核 ``metadata.<key>``
    读的是用户值——同名会让 Store 与真源两侧判定相反，过滤结果静默出错。在此失败响亮。
    """
    if not metadata:
        return
    clash = sorted(set(metadata) & RESERVED_METADATA_KEYS)
    if clash:
        raise ValidationError(f"metadata 不得使用系统保留 key：{clash}")


def _reject_non_scalar_metadata(metadata: dict[str, Any] | None) -> None:
    """写入/更新边界只接受 JSON 标量与字符串数组。

    嵌套 dict/混合类型数组在各后端语义不一：ES 会把嵌套对象展开成 ``metadata.a.b``
    另建 mapping，Milvus 的 JSON 字段能存但过滤算子对其未定义，UnitReader 又会把
    list 当集合字段走成员语义。三方各行其是且无一致契约，因此在入口挡住。
    字符串数组是例外——tags 类用法有明确的成员包含语义（``json_contains`` / ``term``）。
    """
    if not metadata:
        return
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            continue
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            continue
        raise ValidationError(
            f"metadata[{key!r}] 仅支持 JSON 标量或字符串数组，收到 {type(value).__name__}"
        )


def _policy_bool(policy: PolicyManager, key: str, *, default: bool = False) -> bool:
    try:
        raw = policy.get(key)
    except PolicyError:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _missing_required_space(
    policy: PolicyManager,
    target: Scope,
    require_space: bool,
) -> bool:
    if not require_space:
        return False
    if target == _ROOT:
        return False
    if target.space:
        return False
    return _policy_bool(policy, "scope.require_space")


def _write_permission_context(
    scope: Scope,
    tags: list[str] | None,
    metadata: dict[str, Any] | None,
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
    filters: FilterExpr | None,
) -> PermissionContext:
    """构造查询鉴权上下文：按 **S03 的路由取值规则**解析，授权针对真正会执行的 profile。

    S03「查询侧优先读取 ``extensions[route_key]``，其次读等值 filter」是路由取值的
    唯一口径。权限若另立一套（只认 filters），就会出现「按 A 授权、按 B 执行」——
    调用方用 ``filters=general`` + ``extensions=coding`` 即可拿宽松策略的授权去跑
    coding profile。这里与 S03 同源解析，两侧必然指向同一个 delegate。

    含义是 extensions 能决定命中哪条 policy，因此各 profile 的 policy 必须与该
    profile 可触达的数据相匹配——这是部署配置的责任，路由层不做补偿（S03「routing
    不改变授权语义，只选择 delegate」）。
    """
    routed_metadata = _required_filter_metadata(filters)
    # S03「MemoryPipeline 路由规则」——extensions 优先。
    for key, override in context.extensions.items():
        effective = str(override).strip()
        if effective:
            routed_metadata[key] = effective
    return PermissionContext(
        resource_type="query",
        memory_type=routed_metadata.get("memory_type", ""),
        pipeline=routed_metadata.get("pipeline", ""),
        scope=context.scope,
        metadata=routed_metadata,
    )


def _list_permission_contexts(
    scope: Scope,
    memory_types: list[str] | None,
    filters: FilterExpr | None,
    extensions: dict[str, str],
) -> list[PermissionContext]:
    routed_metadata = _required_filter_metadata(filters)
    for key, override in extensions.items():
        effective = str(override).strip()
        if effective:
            routed_metadata[key] = effective
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_memory_type in memory_types or []:
        memory_type = str(raw_memory_type).strip()
        if not memory_type or memory_type in seen:
            continue
        cleaned.append(memory_type)
        seen.add(memory_type)
    if not cleaned:
        return [
            PermissionContext(
                resource_type="memory_list",
                memory_type=routed_metadata.get("memory_type", ""),
                pipeline=routed_metadata.get("pipeline", ""),
                scope=scope,
                metadata=dict(routed_metadata),
            )
        ]
    joined = ",".join(cleaned)
    contexts: list[PermissionContext] = []
    for memory_type in cleaned:
        contexts.append(
            PermissionContext(
                resource_type="memory_list",
                memory_type=memory_type,
                pipeline=routed_metadata.get("pipeline", ""),
                scope=scope,
                metadata={**routed_metadata, "memory_types": joined},
            )
        )
    return contexts


def _normalize_list_extensions(raw: dict[str, str] | None) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("extensions must be a dict")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValidationError("extensions keys must be strings")
        normalized[key] = str(value)
    return normalized


def _permission_route_value(context: PermissionContext, field: str) -> str:
    if field == "memory_type":
        return context.memory_type
    if field == "pipeline":
        return context.pipeline
    if field == "resource_type":
        return context.resource_type
    return str(context.metadata.get(field, "")).strip()


def _list_routing_clauses(
    contexts: list[PermissionContext],
    routing_fields: tuple[str, ...],
    memory_types: list[str] | None,
) -> list[FilterClause]:
    has_memory_types = any(str(value).strip() for value in memory_types or ())
    clauses: list[FilterClause] = []
    for field in routing_fields:
        if field == "memory_type" and has_memory_types:
            continue
        values: set[str] = set()
        for context in contexts:
            value = _permission_route_value(context, field)
            if value:
                values.add(value)
        if len(values) == 1:
            clauses.append(FilterClause(canonical_filter_field(field), FilterOp.EQ, values.pop()))
    return clauses


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


def _space_scope(org: str, space: str) -> Scope:
    return Scope(org=org, space=space)


def _space_target_id(org: str, space: str) -> str:
    return f"{org}/{space}"


def _space_permission_context(resource_type: str, scope: Scope) -> PermissionContext:
    return PermissionContext(resource_type=resource_type, scope=scope)


def _management_permission_context(resource_type: str) -> PermissionContext:
    """管理面（系统配置 / 审计查询）的鉴权上下文。

    这些操作的鉴权 target 是 ``_ROOT``，但「它是管理操作」这件事此前只能从
    「target 恰好是空 scope」间接读出来。显式写成 ``resource_type`` 后，
    PDP 不必再从数据形状反推语义。
    """
    return PermissionContext(resource_type=resource_type, scope=_ROOT)


def _descriptor_attributes(context: PermissionContext | None) -> dict[str, str]:
    """把权限上下文摊平成 :class:`ResourceDescriptor` 的属性映射。

    ``PermissionContext`` 仍是 Engine 侧的契约（``permission_context_for_unit`` /
    ``list_with_permission_contexts`` / ``permission_contexts_for_delete`` 都返回它），
    转换点收在 PEP 这一处：Authorizer 只认 descriptor，不认控制层类型。

    ``metadata`` 先铺、具名字段后覆盖：``memory_type`` 这类字段对已有资源来自真源，
    不能被同名 metadata 项盖掉（F05 §ResourceDescriptor：安全 metadata 来自真源）。
    """
    if context is None:
        return {}
    attributes = {str(key): str(value) for key, value in context.metadata.items()}
    if context.memory_type:
        attributes["memory_type"] = context.memory_type
    if context.pipeline:
        attributes["pipeline"] = context.pipeline
    if context.tags:
        attributes["tags"] = ",".join(context.tags)
    return attributes



class LocalMemoryAPI(MemoryAPI):
    """单进程装配下的统一记忆接口实现（鉴权 + 审计 + 委派）。"""

    def __init__(
        self,
        engine: MemoryEngine,
        permission: PermissionManager,
        authorizer: Authorizer,
        scheduler: Scheduler,
        policy: PolicyManager,
        governor: Governor,
        audit_logger: AuditLogger,
        space: SpaceManager,
    ) -> None:
        self._engine = engine
        # 授权判定归 authorizer（PDP）；permission 在本 PR 只剩 grant/revoke 的授权
        # **记录**写入通道——判定与记录的迁移分属两个 PR（迁移计划 §6.2 第 10 项把
        # 「删除 control 下旧 PermissionManager 安全所有权」划在 PR3）。
        self._perm = permission
        self._authorizer = authorizer
        self._scheduler = scheduler
        self._policy = policy
        self._governor = governor
        self._audit = audit_logger
        self._space = space

    @property
    def space_manager(self) -> SpaceManager:
        return self._space

    def _space_info_if_exists(self, scope: Scope) -> SpaceInfo | None:
        if not scope.org or not scope.space:
            return None
        try:
            return self._space.get(scope.org, scope.space)
        except NotFoundError:
            return None

    def _ensure_space_writable(self, scope: Scope) -> None:
        info = self._space_info_if_exists(scope)
        if info is None:
            return
        if info.status != SpaceStatus.ACTIVE:
            raise ValidationError(
                f"space is not writable: {info.org}/{info.space} status={info.status.value}"
            )

    def _purge_space_memories(self, scope: Scope) -> list[str]:
        return asyncio.run(self._engine.purge_space(scope.org, scope.space))

    # -- 鉴权 + 审计公共点 --------------------------------------------------- #

    def _record_audit(
        self,
        actor: Scope,
        action: str,
        *,
        target_id: str = "",
        target_scope: Scope | None = None,
        decision: str = "allow",
        detail: dict[str, str] | None = None,
    ) -> None:
        payload = dict(detail or {})
        payload.setdefault("decision", decision)
        self._audit.record(
            AuditEvent(
                id=str(uuid.uuid4()),
                actor=actor,
                action=action,
                target_id=target_id,
                layer="api",
                decision=decision,
                occurred_at=datetime.now(timezone.utc),
                detail=payload,
                target=target_scope or _ROOT,
            )
        )

    def _apply_space_policy_context(
        self,
        target: Scope,
        context: PermissionContext | None,
    ) -> PermissionContext | None:
        if not target.org or not target.space:
            return context
        try:
            policy = self._space.get_policy(target.org, target.space)
        except NotFoundError:
            return context
        metadata = dict(context.metadata) if context is not None else {}
        metadata["principal_path"] = policy.principal_path.value
        if context is None:
            return PermissionContext(
                resource_type="",
                scope=target,
                metadata=metadata,
            )
        return replace(context, metadata=metadata)

    def _authorize(
        self,
        security: RequestSecurityContext,
        target: Scope,
        action: Action,
        audit_action: str,
        target_id: str = "",
        *,
        resource_type: str = "",
        context: PermissionContext | None = None,
        require_space: bool = True,
    ) -> dict[str, str]:
        """本层唯一的鉴权点：构造 descriptor + environment，调 Authorizer，落审计。

        ``context`` 是 Engine 侧真源上下文（可为空），在此摊平成 descriptor 属性。
        ``resource_type`` 显式给出时优先——verb 到资源类型的映射归 PEP，不从
        context 的形状反推。
        """
        actor = security.actor
        effective_context = self._apply_space_policy_context(target, context)
        if _missing_required_space(self._policy, target, require_space):
            self._record_audit(
                actor,
                audit_action,
                target_id=target_id,
                target_scope=target,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": "scope.space is required",
                    **_context_detail(effective_context),
                },
            )
            raise ValidationError("scope.space is required")
        resource = ResourceDescriptor(
            action=action,
            resource_type=resource_type
            or (effective_context.resource_type if effective_context else ""),
            scope=target,
            resource_id=target_id,
            attributes=_descriptor_attributes(effective_context),
        )
        environment = AuthorizationEnvironment.from_request(
            security, now=datetime.now(timezone.utc)
        )
        decision = self._authorizer.authorize(
            auth=security.auth, resource=resource, environment=environment
        )
        if not decision.allowed:
            # reason 是稳定 code（DenyReason），rule 标明哪条规则做的判定——两者一起
            # 才能从审计里读出「为什么拒」而不只是「拒了」（F05 §可观测性）。
            self._record_audit(
                actor,
                audit_action,
                target_id=target_id,
                target_scope=target,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": decision.reason.value if decision.reason else "",
                    "permission_rule": decision.rule,
                    **_context_detail(effective_context),
                },
            )
            raise PermissionDeniedError(action.value)
        return {
            "permission_check": "enabled",
            "permission_reason": "permission check passed",
            "permission_rule": decision.rule,
            **_context_detail(effective_context),
        }

    def _log(
        self,
        security: RequestSecurityContext,
        action: str,
        target_id: str = "",
        *,
        target_scope: Scope | None = None,
        decision: str = "allow",
        detail: dict[str, str] | None = None,
    ) -> None:
        self._record_audit(
            security.actor,
            action,
            target_id=target_id,
            target_scope=target_scope,
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
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        return asyncio.run(
            self.write_async(
                content,
                scope,
                source,
                security=security,
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
        security: RequestSecurityContext,
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        _reject_reserved_metadata(metadata)
        _reject_non_scalar_metadata(metadata)
        permission_context = _write_permission_context(scope, tags, metadata)
        auth = self._authorize(
            security,
            scope,
            Action.WRITE,
            "write",
            context=permission_context,
        )
        self._ensure_space_writable(scope)
        units = await self._engine.write(
            content,
            scope,
            source,
            assets=assets,
            tags=tags,
            metadata=metadata,
            occurred_at=occurred_at,
        )
        self._log(security, "write", target_scope=scope, detail=auth)
        return units

    def recall(
        self,
        query: str,
        context: Context,
        *,
        security: RequestSecurityContext,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
        as_of: datetime | None = None,
        top_k: int = 10,
        disclosure: DisclosureLevel = DisclosureLevel.L0,
        with_trajectory: bool = False,
    ) -> RetrievalResult:
        # Context 在边界处拆包：scope 照旧作独立轴下推（鉴权 + 检索），
        # extensions 写入调用级 options 顺 parser 透传给自定义检索模块；
        # Context 对象本身不进内核。约定 key max_tokens（自适应披露预算）
        # 在此解析为 typed int 写入 RetrievalQuery，
        # 并从透传 extensions 中移除，避免与内核已解释的字段重复。
        options = dict(context.extensions)
        max_tokens = _parse_max_tokens(options.pop(EXT_MAX_TOKENS, None))
        rq = RetrievalQuery(
            text=query,
            # RetrievalQuery 边界统一 normalize 旧 list、clause 与 dict DSL。
            filters=filters,
            as_of=as_of,
            top_k=top_k,
            disclosure=disclosure,
            max_tokens=max_tokens,
            with_trajectory=with_trajectory,
            extensions=options,
        )
        # 权限上下文与 RetrievalQuery 共用同一规范化后的 FilterExpr（不重复转换）。
        permission_context = _recall_permission_context(context, rq.filters)
        auth = self._authorize(
            security,
            context.scope,
            Action.READ,
            "recall",
            context=permission_context,
        )
        # 把**授权所依据的路由值**回注为系统谓词：按 memory_type=notes 授的权，这次查询
        # 就只能读到 memory_type=notes 的数据。否则"选哪条策略"与"能读到哪些数据"是两个
        # 独立输入、且都由调用方控制——路由值填宽松策略对应的类型、filters 指向受严格
        # 策略保护的数据，即可用 A 的钥匙开 B 的门。用户表达式作整体 child 并入外层 AND
        # （与 lifecycle/时间谓词同一机制），不会被其内部的 OR 稀释。
        routing_clauses: list[FilterClause] = []
        for field in self._authorizer.routing_fields():
            routed = permission_context.metadata.get(field, "").strip()
            if routed:
                routing_clauses.append(
                    FilterClause(canonical_filter_field(field), FilterOp.EQ, routed)
                )
        if routing_clauses:
            rq.filters = and_merge(rq.filters, routing_clauses)
        result = asyncio.run(self._engine.recall(context.scope, rq))
        self._log(security, "recall", target_scope=context.scope, detail=auth)
        return result

    def list(
        self,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, str] | None = None,
        filters: FilterExpr | list[FilterClause] | dict | None = None,
    ) -> MemoryListResult:
        normalized_extensions = _normalize_list_extensions(extensions)
        normalized_filters = normalize(filters)
        permission_contexts = _list_permission_contexts(
            scope,
            memory_types,
            normalized_filters,
            normalized_extensions,
        )
        auth: dict[str, str] = {}
        for permission_context in permission_contexts:
            auth = self._authorize(
                security,
                scope,
                Action.READ,
                "list",
                context=permission_context,
            )
        routing_clauses = _list_routing_clauses(
            permission_contexts,
            self._authorizer.routing_fields(),
            memory_types,
        )
        effective_filters = and_merge(normalized_filters, routing_clauses)
        result, unit_contexts = asyncio.run(
            self._engine.list_with_permission_contexts(
                scope,
                offset=offset,
                limit=limit,
                memory_types=memory_types,
                extensions=normalized_extensions,
                filters=effective_filters,
            )
        )
        for permission_context in unit_contexts:
            auth = self._authorize(
                security,
                permission_context.scope,
                Action.READ,
                "list",
                permission_context.unit_id,
                context=permission_context,
            )
        if len(permission_contexts) > 1:
            auth["permission_memory_types"] = ",".join(
                context.memory_type for context in permission_contexts
            )
        self._log(
            security,
            "list",
            target_scope=scope,
            detail={
                **auth,
                "count": str(result.count),
                "page_count": str(len(result.items)),
            },
        )
        return result

    def get(
        self,
        unit_id: str,
        scope: Scope,
        *,
        security: RequestSecurityContext,
        as_of: datetime | None = None,
    ) -> MemoryUnit:
        self._authorize(
            security,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        auth = self._authorize(
            security,
            scope,
            Action.READ,
            "get",
            unit_id,
            context=permission_context,
        )
        unit = asyncio.run(self._engine.get(unit_id, scope, as_of))
        self._log(
            security,
            "get",
            unit_id,
            target_scope=scope,
            detail={**auth, "after_unit_id": unit.id},
        )
        return unit

    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, security: RequestSecurityContext
    ) -> MemoryUnit:
        _reject_reserved_metadata(patch.metadata)
        _reject_non_scalar_metadata(patch.metadata)
        self._authorize(
            security,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=_unit_lookup_permission_context(unit_id, scope),
        )
        permission_context = asyncio.run(self._engine.permission_context_for_unit(unit_id, scope))
        auth = self._authorize(
            security,
            scope,
            Action.UPDATE,
            "update",
            unit_id,
            context=permission_context,
        )
        self._ensure_space_writable(scope)
        before = asyncio.run(self._engine.get(unit_id, scope, None))
        unit = asyncio.run(self._engine.update(unit_id, scope, patch))
        self._log(
            security,
            "update",
            unit_id,
            target_scope=scope,
            detail={**auth, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    def delete(self, selector: DeleteSelector, *, security: RequestSecurityContext) -> list[str]:
        selector_is_empty = not selector.unit_ids and not selector.tags and selector.before is None
        if selector_is_empty:
            raise ValidationError("DeleteSelector requires unit_ids, tags, or before")
        # 按 selector 的目标 scope 鉴权 DELETE；未限定 scope（如纯按 id/标签的
        # 跨范围删除）则退到根 scope 闸门，要求更高权限。
        target = selector.scope or _ROOT
        selector_context = _selector_permission_context(selector, target)
        if selector.scope is not None or not selector.unit_ids:
            self._authorize(
                security,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        contexts = asyncio.run(self._engine.permission_contexts_for_delete(selector))
        if not contexts:
            auth = self._authorize(
                security,
                target,
                Action.DELETE,
                "delete",
                context=selector_context,
            )
        else:
            auth = {"permission_check": "enabled", "permission_reason": "permission check passed"}
            for permission_context in contexts:
                unit_auth = self._authorize(
                    security,
                    permission_context.scope,
                    Action.DELETE,
                    "delete",
                    permission_context.unit_id,
                    context=permission_context,
                )
                auth.update(unit_auth)
        deleted = asyncio.run(self._engine.delete(selector))
        self._log(
            security,
            "delete",
            target_scope=target,
            detail={**auth, "before_unit_ids": json.dumps(deleted, ensure_ascii=False)},
        )
        return deleted

    def evolve(
        self,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel = Channel.BACKGROUND,
        *,
        security: RequestSecurityContext,
    ) -> str:
        auth = self._authorize(
            security, scope, Action.WRITE, "evolve", resource_type="evolve_request"
        )
        self._ensure_space_writable(scope)
        job_id = asyncio.run(self._engine.evolve(scope, mode, channel))
        self._log(security, "evolve", target_scope=scope, detail={**auth, "job_id": job_id})
        return job_id

    # -- 任务调度（直达 Scheduler） ----------------------------------------- #

    def job_status(self, job_id: str, *, security: RequestSecurityContext) -> JobInfo:
        # 先取任务（含其 scope），再据 actor 对该 scope 的 READ 权放行
        # （仅可查自身/已授权范围的任务）；status 为只读查询，先取后判权
        # 不产生副作用。任务 scope 来自 Scheduler 这个真源，不是调用方声明的。
        info = self._scheduler.status(job_id)
        auth = self._authorize(
            security, info.scope, Action.READ, "job_status", job_id, resource_type="job"
        )
        self._log(security, "job_status", job_id, target_scope=info.scope, detail=auth)
        return info

    def job_cancel(self, job_id: str, *, security: RequestSecurityContext) -> None:
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE
        # （与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        auth = self._authorize(
            security, info.scope, Action.WRITE, "job_cancel", job_id, resource_type="job"
        )
        self._log(security, "job_cancel", job_id, target_scope=info.scope, detail=auth)
        self._scheduler.cancel(job_id)

    # -- admin（直达 PolicyManager；管理面闸门 = 根 scope 鉴权） ------------- #

    def admin_get(self, key: str, *, security: RequestSecurityContext) -> str:
        auth = self._authorize(
            security,
            _ROOT,
            Action.MANAGE_POLICY,
            "admin_get",
            key,
            context=_management_permission_context("admin"),
        )
        self._log(security, "admin_get", key, target_scope=_ROOT, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, security: RequestSecurityContext) -> None:
        auth = self._authorize(
            security,
            _ROOT,
            Action.MANAGE_POLICY,
            "admin_set",
            key,
            context=_management_permission_context("admin"),
        )
        self._log(security, "admin_set", key, target_scope=_ROOT, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, security: RequestSecurityContext) -> dict[str, str]:
        auth = self._authorize(
            security,
            _ROOT,
            Action.MANAGE_POLICY,
            "admin_all",
            context=_management_permission_context("admin"),
        )
        self._log(security, "admin_all", target_scope=_ROOT, detail=auth)
        return self._policy.all()

    # -- 治理（直达 Governor） ---------------------------------------------- #

    def inspect(
        self, unit_ids: list[str], scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        auth = self._authorize(
            security, scope, Action.READ, "inspect", resource_type="memory_unit"
        )
        self._log(security, "inspect", target_scope=scope, detail=auth)
        return self._governor.inspect(unit_ids, scope)

    def trace(
        self, unit_id: str, scope: Scope, *, security: RequestSecurityContext
    ) -> list[MemoryUnit]:
        auth = self._authorize(
            security, scope, Action.READ, "trace", unit_id, resource_type="memory_unit"
        )
        self._log(security, "trace", unit_id, target_scope=scope, detail=auth)
        return self._governor.trace(unit_id, scope)

    def audit(
        self, filters: dict[str, str], *, security: RequestSecurityContext, limit: int = 100
    ) -> list[AuditEvent]:
        # 审计查询跨 scope，按管理面闸门（根 scope READ）鉴权；
        # 查询本身亦留痕。
        auth = self._authorize(
            security,
            _ROOT,
            Action.READ_AUDIT,
            "audit",
            context=_management_permission_context("audit"),
        )
        self._log(security, "audit", target_scope=_ROOT, detail=auth)
        return self._governor.audit(filters, limit)

    # -- 跨 scope 授权（直达 PermissionManager） ---------------------------- #

    def grant(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        auth = self._authorize(
            security, grant.grantor, Action.SHARE, "grant", resource_type="grant"
        )
        self._log(security, "grant", target_scope=grant.grantor, detail=auth)
        self._perm.grant(grant)

    def revoke(self, grant: Grant, *, security: RequestSecurityContext) -> None:
        auth = self._authorize(
            security,
            grant.grantor,
            Action.REVOKE_SHARE,
            "revoke",
            resource_type="grant",
        )
        self._log(security, "revoke", target_scope=grant.grantor, detail=auth)
        self._perm.revoke(grant)

    # -- Space 管理（直达 SpaceManager） ------------------------------------ #

    def create_space(self, spec: SpaceSpec, *, security: RequestSecurityContext) -> SpaceInfo:
        target = _space_scope(spec.org, spec.space)
        target_id = _space_target_id(spec.org, spec.space)
        auth = self._authorize(
            security,
            Scope(org=spec.org),
            Action.MANAGE_SPACE,
            "create_space",
            target_id,
            context=_space_permission_context("space", target),
            require_space=False,
        )
        info = self._space.create(spec)
        self._log(security, "create_space", target_id, target_scope=target, detail=auth)
        return info

    def get_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "get_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.get(org, space)
        self._log(security, "get_space", target_id, target_scope=target, detail=auth)
        return info

    def list_spaces(
        self,
        org: str,
        *,
        security: RequestSecurityContext,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        target = Scope(org=org)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "list_spaces",
            org,
            context=_space_permission_context("space_list", target),
            require_space=False,
        )
        spaces = self._space.list(org, status=status, limit=limit, cursor=cursor)
        self._log(
            security,
            "list_spaces",
            org,
            target_scope=target,
            detail={**auth, "count": str(len(spaces))},
        )
        return spaces

    def update_space(
        self, org: str, space: str, patch: SpacePatch, *, security: RequestSecurityContext
    ) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "update_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.update(org, space, patch)
        self._log(security, "update_space", target_id, target_scope=target, detail=auth)
        return info

    def archive_space(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "archive_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.archive(org, space)
        self._log(security, "archive_space", target_id, target_scope=target, detail=auth)
        return info

    def delete_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        mode: DeleteMode = DeleteMode.PURGE,
    ) -> SpaceDeleteResult:
        if mode != DeleteMode.PURGE:
            raise ValidationError("delete_space currently supports DeleteMode.PURGE only")
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "delete_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        purged = self._purge_space_memories(target)
        result = self._space.delete(org, space)
        result.deleted_counts["memory"] = result.deleted_counts.get("memory", 0) + len(purged)
        result.deleted_counts["index"] = result.deleted_counts.get("index", 0) + len(purged)
        result.deleted_counts["kv"] = result.deleted_counts.get("kv", 0) + len(purged)
        self._log(
            security,
            "delete_space",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "deleted_memory_ids": json.dumps(purged, ensure_ascii=False),
                "deleted_counts": json.dumps(result.deleted_counts, ensure_ascii=False),
            },
        )
        return result

    def export_space(
        self,
        org: str,
        space: str,
        *,
        security: RequestSecurityContext,
        include_audit: bool = True,
    ) -> str:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "export_space",
            target_id,
            context=_space_permission_context("space_export", target),
        )
        export_id = self._space.export(org, space, include_audit=include_audit)
        self._log(
            security,
            "export_space",
            target_id,
            target_scope=target,
            detail={**auth, "export_id": export_id, "include_audit": str(include_audit)},
        )
        return export_id

    def space_usage(self, org: str, space: str, *, security: RequestSecurityContext) -> SpaceUsage:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "space_usage",
            target_id,
            context=_space_permission_context("space_usage", target),
        )
        usage = self._space.usage(org, space)
        self._log(
            security,
            "space_usage",
            target_id,
            target_scope=target,
            detail={
                **auth,
                "memory_count": str(usage.memory_count),
                "message_count": str(usage.message_count),
                "storage_bytes": str(usage.storage_bytes),
            },
        )
        return usage

    def get_space_policy(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "get_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        policy = self._space.get_policy(org, space)
        self._log(security, "get_space_policy", target_id, target_scope=target, detail=auth)
        return policy

    def set_space_policy(
        self, org: str, space: str, policy: SpacePolicy, *, security: RequestSecurityContext
    ) -> SpacePolicy:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "set_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        updated = self._space.set_policy(org, space, policy)
        self._log(
            security,
            "set_space_policy",
            target_id,
            target_scope=target,
            detail={**auth, "principal_path": updated.principal_path.value},
        )
        return updated

    def list_space_members(
        self, org: str, space: str, *, security: RequestSecurityContext
    ) -> list[SpaceMember]:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.READ,
            "list_space_members",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        members = self._space.list_members(org, space)
        self._log(
            security,
            "list_space_members",
            target_id,
            target_scope=target,
            detail={**auth, "count": str(len(members))},
        )
        return members

    def add_space_member(
        self, org: str, space: str, member: SpaceMember, *, security: RequestSecurityContext
    ) -> None:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "add_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._space.add_member(org, space, member)
        self._log(
            security,
            "add_space_member",
            target_id,
            target_scope=target,
            detail={**auth, "member_role": member.role},
        )

    def remove_space_member(
        self, org: str, space: str, member: Scope, *, security: RequestSecurityContext
    ) -> None:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            security,
            target,
            Action.MANAGE_SPACE,
            "remove_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._space.remove_member(org, space, member)
        self._log(security, "remove_space_member", target_id, target_scope=target, detail=auth)
