""":class:`~api.memory_api.MemoryAPI` 的单进程实现（``LocalMemoryAPI``）+ 装配。

``LocalMemoryAPI`` 是鉴权与审计的执行点（PEP）：每个涉及租户数据/治理的
方法先构造权限上下文并调用 ``PermissionManager.check(..., context=...)``，
不通过抛 :class:`~common.errors.PermissionDeniedError`，通过后落入口审计并把
已鉴权的 target scope 透传到引擎/各控制算子（identity 不下沉）。同步方法以
``asyncio.run`` 桥接引擎的异步协程，供 CLI/脚本使用。各控制算子按其
抽象基类型注入。

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
from common.type_def import (
    EXT_MAX_TOKENS,
    RESERVED_METADATA_KEYS,
    TRANSIENT_METADATA_KEYS,
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
    Action,
    BatchWriteItem,
    BatchWriteOutcome,
    BatchWriteResult,
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


def _reject_invalid_content(content: object) -> None:
    """写入边界拒绝非 str / 空 / 纯空白 content。

    单条 write 曾把校验漏到 engine ``content.encode`` 与 Normalizer：``None`` 变
    ``AttributeError``，``""`` 靠 ``b""`` 假值副作用才成 ValidationError，``"   "``
    则静默落盘。与 batch 路径统一在 API 入口失败响亮。
    """
    if not isinstance(content, str):
        raise ValidationError(f"content must be str, got {type(content).__name__}")
    if not content.strip():
        raise ValidationError("content must be a non-empty str")


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
    瞬态 key（``TRANSIENT_METADATA_KEYS``）是例外——它们透传原始对象到 storage 层消费，
    不落盘，因此允许任意类型。
    """
    if not metadata:
        return
    for key, value in metadata.items():
        # 瞬态 key 允许任意类型（对象透传，不落盘）
        if key in TRANSIENT_METADATA_KEYS:
            continue
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
    meta = {}
    for key, value in (metadata or {}).items():
        # 瞬态 key 保留原始对象（透传到 storage 层消费，不落盘）
        if key in TRANSIENT_METADATA_KEYS:
            meta[key] = value
        else:
            meta[key] = str(value)
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
        # 瞬态 key 透传原值，不 str 化
        if key in TRANSIENT_METADATA_KEYS:
            routed_metadata[key] = override
            continue
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
    extensions: dict[str, Any],
) -> list[PermissionContext]:
    routed_metadata = _required_filter_metadata(filters)
    for key, override in extensions.items():
        # 瞬态 key 透传原值，不 str 化
        if key in TRANSIENT_METADATA_KEYS:
            routed_metadata[key] = override
            continue
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


def _normalize_list_extensions(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("extensions must be a dict")
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValidationError("extensions keys must be strings")
        # 瞬态 key 保留原始对象（如 db_query_service / encryption_port），不强制 str 化
        if key in TRANSIENT_METADATA_KEYS:
            normalized[key] = value
        else:
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
            clauses.append(
                FilterClause(canonical_filter_field(field), FilterOp.EQ, values.pop())
            )
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
        space: SpaceManager,
    ) -> None:
        self._engine = engine
        self._perm = permission
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
        identity: Scope,
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
                actor=identity,
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
        identity: Scope,
        target: Scope,
        action: Action,
        audit_action: str,
        target_id: str = "",
        *,
        context: PermissionContext | None = None,
        check_permission: bool = True,
        require_space: bool = True,
    ) -> dict[str, str]:
        effective_context = self._apply_space_policy_context(target, context)
        if _missing_required_space(self._policy, target, require_space):
            self._record_audit(
                identity,
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
        if not check_permission:
            return {
                "permission_check": "disabled",
                "permission_reason": "permission check disabled",
                **_context_detail(effective_context),
            }
        if not self._perm.check(identity, target, action, context=effective_context):
            self._record_audit(
                identity,
                audit_action,
                target_id=target_id,
                target_scope=target,
                decision="deny",
                detail={
                    "permission_check": "enabled",
                    "permission_reason": f"permission denied for action={action.value}",
                    **_context_detail(effective_context),
                },
            )
            raise PermissionDeniedError(action.value)
        return {
            "permission_check": "enabled",
            "permission_reason": "permission check passed",
            **_context_detail(effective_context),
        }

    def _log(
        self,
        identity: Scope,
        action: str,
        target_id: str = "",
        *,
        target_scope: Scope | None = None,
        decision: str = "allow",
        detail: dict[str, str] | None = None,
    ) -> None:
        self._record_audit(
            identity,
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
        identity: Scope,
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
        assets: list[str] | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> list[MemoryUnit]:
        _reject_invalid_content(content)
        _reject_reserved_metadata(metadata)
        _reject_non_scalar_metadata(metadata)
        permission_context = _write_permission_context(scope, tags, metadata)
        auth = self._authorize(
            identity,
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
        self._log(identity, "write", target_scope=scope, detail=auth)
        return units

    @staticmethod
    def _batch_error_item(item: object) -> BatchWriteItem:
        if isinstance(item, BatchWriteItem):
            return item
        return BatchWriteItem(content="")

    @staticmethod
    def _merge_batch_tags(
        defaults: list[str] | None, item_tags: list[str] | None
    ) -> list[str] | None:
        if defaults is None and item_tags is None:
            return None
        merged: list[str] = []
        for tag in [*(defaults or []), *(item_tags or [])]:
            if tag not in merged:
                merged.append(tag)
        return merged

    @classmethod
    def _normalize_batch_item(
        cls,
        item: object,
        *,
        scope: Scope | None,
        source: Modality,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
        occurred_at: datetime | None,
        stream_id: str,
    ) -> BatchWriteItem:
        if not isinstance(item, BatchWriteItem):
            raise ValidationError("batch item must be BatchWriteItem")
        _reject_invalid_content(item.content)
        target_scope = item.scope if item.scope is not None else scope
        if not isinstance(target_scope, Scope):
            raise ValidationError("batch item scope is required")
        item_source = item.source if item.source is not None else source
        if not isinstance(item_source, Modality):
            raise ValidationError("batch item source must be Modality")
        if item.assets is not None and (
            not isinstance(item.assets, list)
            or any(not isinstance(asset, str) for asset in item.assets)
        ):
            raise ValidationError("batch item assets must be list[str]")
        for values, name in ((tags, "tags"), (item.tags, "item tags")):
            if values is not None and (
                not isinstance(values, list) or any(not isinstance(value, str) for value in values)
            ):
                raise ValidationError(f"batch {name} must be list[str]")
        if item.metadata is not None and not isinstance(item.metadata, dict):
            raise ValidationError("batch item metadata must be dict")
        if item.occurred_at is not None and not isinstance(item.occurred_at, datetime):
            raise ValidationError("batch item occurred_at must be datetime")
        merged_metadata = {**(metadata or {}), **(item.metadata or {})}
        _reject_reserved_metadata(merged_metadata)
        _reject_non_scalar_metadata(merged_metadata)
        if item.stream_id and not isinstance(item.stream_id, str):
            raise ValidationError("batch item stream_id must be str")
        if item.sequence is not None and not isinstance(item.sequence, int):
            raise ValidationError("batch item sequence must be int")
        if not isinstance(item.idempotency_key, str):
            raise ValidationError("batch item idempotency_key must be str")
        return BatchWriteItem(
            content=item.content,
            scope=target_scope,
            source=item_source,
            assets=list(item.assets) if item.assets is not None else None,
            tags=cls._merge_batch_tags(tags, item.tags),
            metadata=merged_metadata or None,
            occurred_at=item.occurred_at if item.occurred_at is not None else occurred_at,
            stream_id=item.stream_id or stream_id,
            sequence=item.sequence,
            idempotency_key=item.idempotency_key,
        )

    @staticmethod
    def _batch_outcome(
        index: int, item: object, error: Exception, *, error_type: str | None = None
    ) -> BatchWriteOutcome:
        return BatchWriteOutcome(
            index=index,
            item=LocalMemoryAPI._batch_error_item(item),
            error=str(error),
            error_type=error_type or type(error).__name__,
        )

    def batch_write(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        return asyncio.run(
            self.batch_write_async(
                items,
                scope,
                source,
                identity=identity,
                tags=tags,
                metadata=metadata,
                occurred_at=occurred_at,
                stream_id=stream_id,
                continue_on_error=continue_on_error,
            )
        )

    async def batch_write_async(
        self,
        items: list[BatchWriteItem],
        scope: Scope | None = None,
        source: Modality = Modality.TEXT,
        *,
        identity: Scope,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        stream_id: str = "",
        continue_on_error: bool = True,
    ) -> BatchWriteResult:
        if not isinstance(items, list) or not items:
            raise ValidationError("batch items must be a non-empty list")
        if scope is not None and not isinstance(scope, Scope):
            raise ValidationError("batch scope must be Scope")
        if not isinstance(source, Modality):
            raise ValidationError("batch source must be Modality")
        if tags is not None and (
            not isinstance(tags, list) or any(not isinstance(value, str) for value in tags)
        ):
            raise ValidationError("batch tags must be list[str]")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("batch metadata must be dict")
        if not isinstance(stream_id, str):
            raise ValidationError("batch stream_id must be str")
        if occurred_at is not None and not isinstance(occurred_at, datetime):
            raise ValidationError("batch occurred_at must be datetime")
        _reject_reserved_metadata(metadata)
        _reject_non_scalar_metadata(metadata)

        outcomes: dict[int, BatchWriteOutcome] = {}
        ready: list[tuple[int, BatchWriteItem]] = []
        seen_sequences: set[tuple[str, str, str, str, str, str, int]] = set()
        stopped_index: int | None = None

        for index, raw_item in enumerate(items):
            try:
                item = self._normalize_batch_item(
                    raw_item,
                    scope=scope,
                    source=source,
                    tags=tags,
                    metadata=metadata,
                    occurred_at=occurred_at,
                    stream_id=stream_id,
                )
                if item.sequence is not None:
                    sequence_key = (
                        item.scope.org,
                        item.scope.space,
                        item.scope.user,
                        item.scope.agent,
                        item.scope.session,
                        item.stream_id,
                        item.sequence,
                    )
                    if sequence_key in seen_sequences:
                        raise ValidationError(
                            "duplicate sequence within the same scope and stream"
                        )
                    seen_sequences.add(sequence_key)
                ready.append((index, item))
            except Exception as exc:
                if not isinstance(exc, (ValidationError, PermissionDeniedError, PolicyError)):
                    raise
                outcomes[index] = self._batch_outcome(index, raw_item, exc)
                error_scope = (
                    raw_item.scope
                    if isinstance(raw_item, BatchWriteItem) and isinstance(raw_item.scope, Scope)
                    else scope
                )
                self._log(
                    identity,
                    "write",
                    target_scope=error_scope,
                    decision="error",
                    detail={"error": str(exc), "error_type": type(exc).__name__},
                )
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            for index in range(stopped_index + 1, len(items)):
                outcomes[index] = BatchWriteOutcome(
                    index=index,
                    item=self._batch_error_item(items[index]),
                    error="skipped after previous item failed",
                    error_type="Skipped",
                )

        authorized: list[tuple[int, BatchWriteItem, dict[str, str]]] = []
        for index, item in ready:
            permission_context = _write_permission_context(item.scope, item.tags, item.metadata)
            try:
                auth = self._authorize(
                    identity,
                    item.scope,
                    Action.WRITE,
                    "write",
                    context=permission_context,
                )
                self._ensure_space_writable(item.scope)
                authorized.append((index, item, auth))
            except (PermissionDeniedError, ValidationError, PolicyError) as exc:
                outcomes[index] = self._batch_outcome(index, item, exc)
                if not isinstance(exc, PermissionDeniedError):
                    self._log(
                        identity,
                        "write",
                        target_scope=item.scope,
                        decision="error",
                        detail={"error": str(exc), "error_type": type(exc).__name__},
                    )
                if not continue_on_error:
                    stopped_index = index
                    break

        if stopped_index is not None:
            for index in range(stopped_index + 1, len(items)):
                outcomes.setdefault(
                    index,
                    BatchWriteOutcome(
                        index=index,
                        item=self._batch_error_item(items[index]),
                        error="skipped after previous item failed",
                        error_type="Skipped",
                    ),
                )
            authorized = [entry for entry in authorized if entry[0] < stopped_index]

        if authorized:
            engine_result = await self._engine.batch_write(
                [item for _, item, _ in authorized],
                continue_on_error=continue_on_error,
            )
            for engine_outcome, (index, item, auth) in zip(engine_result.outcomes, authorized):
                engine_outcome.index = index
                engine_outcome.item = item
                outcomes[index] = engine_outcome
                self._log(
                    identity,
                    "write",
                    target_scope=item.scope,
                    decision="allow" if not engine_outcome.error else "error",
                    detail={
                        **auth,
                        "error": engine_outcome.error,
                        "error_type": engine_outcome.error_type,
                    },
                )

        return BatchWriteResult(
            outcomes=[outcomes[index] for index in range(len(items))]
        )

    def recall(
        self,
        query: str,
        context: Context,
        *,
        identity: Scope,
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
            identity,
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
        for field in self._perm.routing_fields():
            routed = permission_context.metadata.get(field, "").strip()
            if routed:
                routing_clauses.append(
                    FilterClause(canonical_filter_field(field), FilterOp.EQ, routed)
                )
        if routing_clauses:
            rq.filters = and_merge(rq.filters, routing_clauses)
        result = asyncio.run(self._engine.recall(context.scope, rq))
        self._log(identity, "recall", target_scope=context.scope, detail=auth)
        return result

    def list(
        self,
        scope: Scope,
        *,
        identity: Scope,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        extensions: dict[str, Any] | None = None,
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
                identity,
                scope,
                Action.READ,
                "list",
                context=permission_context,
            )
        routing_clauses = _list_routing_clauses(
            permission_contexts,
            self._perm.routing_fields(),
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
                identity,
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
            identity,
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
        self._log(
            identity,
            "get",
            unit_id,
            target_scope=scope,
            detail={**auth, "after_unit_id": unit.id},
        )
        return unit

    def update(
        self, unit_id: str, scope: Scope, patch: MemoryPatch, *, identity: Scope
    ) -> MemoryUnit:
        _reject_reserved_metadata(patch.metadata)
        _reject_non_scalar_metadata(patch.metadata)
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
        self._ensure_space_writable(scope)
        before = asyncio.run(self._engine.get(unit_id, scope, None))
        unit = asyncio.run(self._engine.update(unit_id, scope, patch))
        self._log(
            identity,
            "update",
            unit_id,
            target_scope=scope,
            detail={**auth, "before_unit_id": before.id, "after_unit_id": unit.id},
        )
        return unit

    def delete(self, selector: DeleteSelector, *, identity: Scope) -> list[str]:
        selector_is_empty = (
            not selector.unit_ids and not selector.tags and selector.before is None
        )
        if selector_is_empty:
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
        identity: Scope,
    ) -> str:
        auth = self._authorize(identity, scope, Action.WRITE, "evolve")
        self._ensure_space_writable(scope)
        job_id = asyncio.run(self._engine.evolve(scope, mode, channel))
        self._log(identity, "evolve", target_scope=scope, detail={**auth, "job_id": job_id})
        return job_id

    # -- 任务调度（直达 Scheduler） ----------------------------------------- #

    def job_status(self, job_id: str, *, identity: Scope) -> JobInfo:
        # 先取任务（含其 scope），再据 identity 对该 scope 的 READ 权放行
        # （仅可查自身/已授权范围的任务）；status 为只读查询，先取后判权
        # 不产生副作用。
        info = self._scheduler.status(job_id)
        auth = self._authorize(identity, info.scope, Action.READ, "job_status", job_id)
        self._log(identity, "job_status", job_id, target_scope=info.scope, detail=auth)
        return info

    def job_cancel(self, job_id: str, *, identity: Scope) -> None:
        # 取消即对该任务范围的写动作，按其 scope 鉴权 WRITE
        # （与 evolve 触发一致）。
        info = self._scheduler.status(job_id)
        auth = self._authorize(identity, info.scope, Action.WRITE, "job_cancel", job_id)
        self._log(identity, "job_cancel", job_id, target_scope=info.scope, detail=auth)
        self._scheduler.cancel(job_id)

    # -- admin（直达 PolicyManager；管理面闸门 = 根 scope 鉴权） ------------- #

    def admin_get(self, key: str, *, identity: Scope) -> str:
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_get", key)
        self._log(identity, "admin_get", key, target_scope=_ROOT, detail=auth)
        return self._policy.get(key)

    def admin_set(self, key: str, value: str, *, identity: Scope) -> None:
        auth = self._authorize(identity, _ROOT, Action.WRITE, "admin_set", key)
        self._log(identity, "admin_set", key, target_scope=_ROOT, detail=auth)
        self._policy.set(key, value)

    def admin_all(self, *, identity: Scope) -> dict[str, str]:
        auth = self._authorize(identity, _ROOT, Action.READ, "admin_all")
        self._log(identity, "admin_all", target_scope=_ROOT, detail=auth)
        return self._policy.all()

    # -- 治理（直达 Governor） ---------------------------------------------- #

    def inspect(
        self, unit_ids: list[str], scope: Scope, *, identity: Scope
    ) -> list[MemoryUnit]:
        auth = self._authorize(identity, scope, Action.READ, "inspect")
        self._log(identity, "inspect", target_scope=scope, detail=auth)
        return self._governor.inspect(unit_ids, scope)

    def trace(self, unit_id: str, scope: Scope, *, identity: Scope) -> list[MemoryUnit]:
        auth = self._authorize(identity, scope, Action.READ, "trace", unit_id)
        self._log(identity, "trace", unit_id, target_scope=scope, detail=auth)
        return self._governor.trace(unit_id, scope)

    def audit(
        self, filters: dict[str, str], *, identity: Scope, limit: int = 100
    ) -> list[AuditEvent]:
        # 审计查询跨 scope，按管理面闸门（根 scope READ）鉴权；
        # 查询本身亦留痕。
        auth = self._authorize(identity, _ROOT, Action.READ, "audit")
        self._log(identity, "audit", target_scope=_ROOT, detail=auth)
        return self._governor.audit(filters, limit)

    # -- 跨 scope 授权（直达 PermissionManager） ---------------------------- #

    def grant(self, grant: Grant, *, identity: Scope) -> None:
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "grant")
        self._log(identity, "grant", target_scope=grant.grantor, detail=auth)
        self._perm.grant(grant)

    def revoke(self, grant: Grant, *, identity: Scope) -> None:
        auth = self._authorize(identity, grant.grantor, Action.SHARE, "revoke")
        self._log(identity, "revoke", target_scope=grant.grantor, detail=auth)
        self._perm.revoke(grant)

    # -- Space 管理（直达 SpaceManager） ------------------------------------ #

    def create_space(self, spec: SpaceSpec, *, identity: Scope) -> SpaceInfo:
        target = _space_scope(spec.org, spec.space)
        target_id = _space_target_id(spec.org, spec.space)
        auth = self._authorize(
            identity,
            Scope(org=spec.org),
            Action.WRITE,
            "create_space",
            target_id,
            context=_space_permission_context("space", target),
            require_space=False,
        )
        info = self._space.create(spec)
        self._log(identity, "create_space", target_id, target_scope=target, detail=auth)
        return info

    def get_space(self, org: str, space: str, *, identity: Scope) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.get(org, space)
        self._log(identity, "get_space", target_id, target_scope=target, detail=auth)
        return info

    def list_spaces(
        self,
        org: str,
        *,
        identity: Scope,
        status: SpaceStatus | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[SpaceInfo]:
        target = Scope(org=org)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_spaces",
            org,
            context=_space_permission_context("space_list", target),
            require_space=False,
        )
        spaces = self._space.list(org, status=status, limit=limit, cursor=cursor)
        self._log(
            identity,
            "list_spaces",
            org,
            target_scope=target,
            detail={**auth, "count": str(len(spaces))},
        )
        return spaces

    def update_space(
        self, org: str, space: str, patch: SpacePatch, *, identity: Scope
    ) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "update_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.update(org, space, patch)
        self._log(identity, "update_space", target_id, target_scope=target, detail=auth)
        return info

    def archive_space(self, org: str, space: str, *, identity: Scope) -> SpaceInfo:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "archive_space",
            target_id,
            context=_space_permission_context("space", target),
        )
        info = self._space.archive(org, space)
        self._log(identity, "archive_space", target_id, target_scope=target, detail=auth)
        return info

    def delete_space(
        self,
        org: str,
        space: str,
        *,
        identity: Scope,
        mode: DeleteMode = DeleteMode.PURGE,
    ) -> SpaceDeleteResult:
        if mode != DeleteMode.PURGE:
            raise ValidationError("delete_space currently supports DeleteMode.PURGE only")
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.DELETE,
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
            identity,
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
        identity: Scope,
        include_audit: bool = True,
    ) -> str:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "export_space",
            target_id,
            context=_space_permission_context("space_export", target),
        )
        export_id = self._space.export(org, space, include_audit=include_audit)
        self._log(
            identity,
            "export_space",
            target_id,
            target_scope=target,
            detail={**auth, "export_id": export_id, "include_audit": str(include_audit)},
        )
        return export_id

    def space_usage(self, org: str, space: str, *, identity: Scope) -> SpaceUsage:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "space_usage",
            target_id,
            context=_space_permission_context("space_usage", target),
        )
        usage = self._space.usage(org, space)
        self._log(
            identity,
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

    def get_space_policy(self, org: str, space: str, *, identity: Scope) -> SpacePolicy:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "get_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        policy = self._space.get_policy(org, space)
        self._log(identity, "get_space_policy", target_id, target_scope=target, detail=auth)
        return policy

    def set_space_policy(
        self, org: str, space: str, policy: SpacePolicy, *, identity: Scope
    ) -> SpacePolicy:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.UPDATE,
            "set_space_policy",
            target_id,
            context=_space_permission_context("space_policy", target),
        )
        updated = self._space.set_policy(org, space, policy)
        self._log(
            identity,
            "set_space_policy",
            target_id,
            target_scope=target,
            detail={**auth, "principal_path": updated.principal_path.value},
        )
        return updated

    def list_space_members(
        self, org: str, space: str, *, identity: Scope
    ) -> list[SpaceMember]:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.READ,
            "list_space_members",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        members = self._space.list_members(org, space)
        self._log(
            identity,
            "list_space_members",
            target_id,
            target_scope=target,
            detail={**auth, "count": str(len(members))},
        )
        return members

    def add_space_member(
        self, org: str, space: str, member: SpaceMember, *, identity: Scope
    ) -> None:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "add_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._space.add_member(org, space, member)
        self._log(
            identity,
            "add_space_member",
            target_id,
            target_scope=target,
            detail={**auth, "member_role": member.role},
        )

    def remove_space_member(
        self, org: str, space: str, member: Scope, *, identity: Scope
    ) -> None:
        target = _space_scope(org, space)
        target_id = _space_target_id(org, space)
        auth = self._authorize(
            identity,
            target,
            Action.SHARE,
            "remove_space_member",
            target_id,
            context=_space_permission_context("space_member", target),
        )
        self._space.remove_member(org, space, member)
        self._log(identity, "remove_space_member", target_id, target_scope=target, detail=auth)
