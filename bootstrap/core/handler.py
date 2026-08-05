"""Verb dispatch — the single code path both the CLI and HTTP surfaces share.

``dispatch(srv, verb, payload) -> (status, body)`` routes a ``(verb, payload)``
to the assembled :class:`~server.Server`'s ``MemoryAPI`` and shapes a JSON-able
envelope the surfaces render. Routing is a table (A20 "route by table"), not an
if/else ladder; domain exceptions map to HTTP-ish status codes.

Scope mapping (DESIGN.md "Two id spaces" / "Mem0 compatibility"): the kernel
scopes by ``tenant_id`` + optional ``space`` / ``space_id`` + a single
``scope`` string, mapped onto the native
``Scope(org=tenant_id, space=space, user=scope)``. The request shape keeps old
empty-space payloads compatible, and still describes the **target** scope
("which resource"); the **actor** ("who is asking") no longer comes from the
payload at all — it comes from the auth layer's ``AuthContext``
(security.md §9 铁律 #1). Payloads that still carry ``actor_*`` fields are
rejected outright rather than silently ignored.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Any, Callable

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"
)
if _SRC not in sys.path:
    sys.path.append(_SRC)

_errors_module = import_module("common.errors")
AgentMemoryError = _errors_module.AgentMemoryError
AuthenticationError = _errors_module.AuthenticationError
ConflictError = _errors_module.ConflictError
NotFoundError = _errors_module.NotFoundError
PermissionDeniedError = _errors_module.PermissionDeniedError
PolicyError = _errors_module.PolicyError
ValidationError = _errors_module.ValidationError

_type_def_module = import_module("common.type_def")
AuditEvent = _type_def_module.AuditEvent
Context = _type_def_module.Context
EXT_MAX_TOKENS = _type_def_module.EXT_MAX_TOKENS
MEMORY_KEY_PREFIX = _type_def_module.MEMORY_KEY_PREFIX
MemoryUnit = _type_def_module.MemoryUnit
Modality = _type_def_module.Modality
Scope = _type_def_module.Scope
get_current = import_module("common.security.types").get_current
EvolveMode = import_module("construction").EvolveMode

_control_types_module = import_module("control.types")
Action = _control_types_module.Action
DeleteMode = _control_types_module.DeleteMode
DeleteSelector = _control_types_module.DeleteSelector
Grant = _control_types_module.Grant
MemoryPatch = _control_types_module.MemoryPatch
PrincipalPath = _control_types_module.PrincipalPath
SpaceMember = _control_types_module.SpaceMember
SpacePatch = _control_types_module.SpacePatch
SpacePolicy = _control_types_module.SpacePolicy
SpaceSpec = _control_types_module.SpaceSpec
SpaceStatus = _control_types_module.SpaceStatus
DisclosureLevel = import_module("retrieval.types").DisclosureLevel

Body = dict[str, Any]

_STATUS = {
    NotFoundError: 404,
    AuthenticationError: 401,  # 不知道你是谁
    PermissionDeniedError: 403,  # 知道你是谁，但不许
    ConflictError: 409,
    ValidationError: 400,
    PolicyError: 400,
}


def _parse_positive_int(value: Any, *, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return parsed


def _parse_non_negative_int(value: Any, *, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a non-negative integer") from None
    if parsed < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return parsed


def _parse_string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value]
    else:
        raise ValidationError("memory_types must be a list or comma-separated string")
    return [item for item in items if item]


def _parse_extensions(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("extensions must be a dict")
    if any(not isinstance(key, str) for key in value):
        raise ValidationError("extensions keys must be strings")
    return {key: str(item) for key, item in value.items()}


def _space_value(payload: Body, *, prefix: str = "") -> str:
    raw = payload.get(f"{prefix}space", payload.get(f"{prefix}space_id", ""))
    return "" if raw is None else str(raw)


def _require_space(payload: Body) -> str:
    value = _space_value(payload)
    if not value:
        raise ValidationError("missing required field: 'space'")
    return value


def _target_scope(payload: Body) -> Scope:
    """Target scope from tenant_id + space + scope."""
    return Scope(
        org=str(payload.get("tenant_id", "default")) or "default",
        space=_space_value(payload),
        user=str(payload.get("scope", "")),
    )


def _identity() -> Scope:
    """调用方身份——来自认证层产出的可信上下文，**不来自 payload**。

    security.md §9 铁律 #1：身份来自上下文，不来自参数。本函数的前身
    ``_actor_scope(payload)`` 直接读 ``payload["actor_tenant_id"]`` 等字段，
    任何人提交 ``{"actor_scope": "victim"}`` 即可读到 victim 的记忆；提交
    ``{"actor_tenant_id": " "}`` 更是拿到空 ``Scope()``，命中
    ``SQLitePermissionManager.check`` 的 platform-admin 全局放行。

    不接受任何参数是刻意的：签名上就不给「从别处取身份」留位置。
    """
    ctx = get_current()
    if ctx is None:
        # 中间件未挂载或漏挂——fail-closed，绝不回退到 payload 或默认身份。
        # 装配错误应该让所有请求失败，而不是让所有请求以未知身份成功。
        raise AuthenticationError("authentication required")
    return ctx.actor


# ``actor_space`` / ``actor_space_id`` 是 space 五维化时一并加进来的伪造面：
# 声明字段每多一维，可冒充的主体就多一维。禁止列表必须与 ``Scope`` 的维数同步——
# 将来 ``Scope`` 再加维，这里要跟着加。
_FORBIDDEN_IDENTITY_KEYS = (
    "actor_tenant_id",
    "actor_space",
    "actor_space_id",
    "actor_scope",
    "actor_agent",
    "actor_session",
)

# ``audit`` verb 用 actor_agent / actor_session 作**查询过滤谓词**（筛历史事件的
# 操作者是谁），与身份声明同名但语义不同——它们不参与本次请求的授权。
# 对该 verb 只拒其余四个（它们不是 audit 的过滤键，出现在那里同样是误以为能声明身份）。
_AUDIT_FILTER_KEYS = ("actor_agent", "actor_session")


def _reject_claimed_identity(payload: Body, allow: tuple[str, ...] = ()) -> None:
    """payload 里出现身份声明字段一律报错，不静默忽略。

    静默忽略会让「我传了 actor_scope」被误认为仍然生效，写出错误的安全认知；
    显式报错迫使调用方改用认证凭据。
    """
    present = [key for key in _FORBIDDEN_IDENTITY_KEYS if key in payload and key not in allow]
    if present:
        raise ValidationError(
            f"identity must come from credentials, not payload: {sorted(present)}"
        )


def _require(payload: Body, key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        raise ValidationError(f"missing required field: {key!r}")
    return value


def _unit_view(unit: MemoryUnit) -> Body:
    return {
        "item_id": unit.id,
        "content": unit.content,
        "tags": list(unit.tags),
        "tier": unit.tier.value,
        "lifecycle": unit.lifecycle.value,
        "assets": list(unit.assets),
    }


def _scope_view(scope: Scope) -> Body:
    return {
        "org": scope.org,
        "space": scope.space,
        "user": scope.user,
        "agent": scope.agent,
        "session": scope.session,
    }


def _event_view(ev: AuditEvent) -> Body:
    return {
        "action": ev.action,
        "target_id": ev.target_id,
        "layer": ev.layer,
        "actor": _scope_view(ev.actor),
        "target": _scope_view(ev.target),
        "decision": ev.decision,
        "detail": dict(ev.detail),
    }


def _string_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("expected object")
    return {str(k): str(v) for k, v in value.items()}


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _enum_value(enum_cls: Any, value: Any, *, name: str) -> Any:
    try:
        return enum_cls(str(value))
    except ValueError:
        allowed = ", ".join(item.value for item in enum_cls)
        raise ValidationError(f"{name} must be one of: {allowed}") from None


def _space_policy(payload: Body) -> SpacePolicy:
    raw = payload.get("policy")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError("policy must be an object")
    principal_path = str(payload.get("principal_path", raw.get("principal_path", "user_agent")))
    return SpacePolicy(
        require_space=_bool_value(raw.get("require_space"), default=False),
        principal_path=_enum_value(PrincipalPath, principal_path, name="principal_path"),
        storage_isolation_strategy=str(raw.get("storage_isolation_strategy", "metadata_filter")),
        retention=_string_map(raw.get("retention")),
        quotas=_string_map(raw.get("quotas")),
        index_profiles=_string_map(raw.get("index_profiles", raw.get("indexes"))),
        pipeline_profiles=_string_map(raw.get("pipeline_profiles", raw.get("pipelines"))),
    )


def _space_info_view(info) -> Body:
    return {
        "org": info.org,
        "space": info.space,
        "display_name": info.display_name,
        "status": info.status.value,
        "principal_path": info.principal_path.value,
        "policy": _space_policy_view(info.policy),
        "metadata": dict(info.metadata),
        "created_at": info.created_at.isoformat() if info.created_at else None,
        "archived_at": info.archived_at.isoformat() if info.archived_at else None,
    }


def _space_policy_view(policy) -> Body:
    return {
        "require_space": policy.require_space,
        "principal_path": policy.principal_path.value,
        "storage_isolation_strategy": policy.storage_isolation_strategy,
        "retention": dict(policy.retention),
        "quotas": dict(policy.quotas),
        "index_profiles": dict(policy.index_profiles),
        "pipeline_profiles": dict(policy.pipeline_profiles),
    }


def _space_member(payload: Body) -> SpaceMember:
    target = _target_scope(payload)
    member_scope = Scope(
        org=str(payload.get("member_tenant_id", target.org)) or target.org,
        space=_space_value(payload, prefix="member_") or target.space,
        user=str(_require(payload, "member")),
        agent=str(payload.get("member_agent", "")),
        session=str(payload.get("member_session", "")),
    )
    return SpaceMember(scope=member_scope, role=str(payload.get("role", "member")) or "member")


def _member_scope(payload: Body) -> Scope:
    return _space_member(payload).scope


def _member_view(member) -> Body:
    return {
        "scope": _scope_view(member.scope),
        "role": member.role,
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "expires_at": member.expires_at.isoformat() if member.expires_at else None,
    }


def _usage_view(usage) -> Body:
    return {
        "org": usage.org,
        "space": usage.space,
        "memory_count": usage.memory_count,
        "message_count": usage.message_count,
        "index_count": usage.index_count,
        "storage_bytes": usage.storage_bytes,
        "audit_count": usage.audit_count,
    }


# --- per-verb handlers ----------------------------------------------------- #


def _add(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    modality = Modality(payload.get("modality", "text"))
    # metadata 透传：infer 等调用级开关经 metadata 下推到引擎（engine.write 从
    # metadata["infer"]=="true" 判定是否同步走 evolve(EXTRACT) 抽取派生记忆）。
    # JSON 标量原样透传（不 str 化）：数值/布尔要保持原生类型才能在索引里建
    # double/boolean mapping 并原生下推；合法性由 API 写入边界统一校验。
    # 显式校验 dict：metadata 为 truthy 非 dict（字符串/列表等畸形 JSON）时兜底为空，
    # 避免下游 .items() 抛 AttributeError → HTTP 500。
    raw_meta = payload.get("metadata")
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    metadata = dict(raw_meta)
    units = srv.api.write(
        _require(payload, "content"),
        scope,
        modality,
        identity=actor,
        tags=payload.get("tags"),
        assets=payload.get("assets"),
        metadata=metadata or None,
    )
    # infer=True 时引擎可能合法返回空：派生记忆全部被 dedup 判为 update/noop
    # （result.created_ids 为空，见 engine.write 的 infer 分支）。
    # 此时不伪造 item_id，
    # 如实返回 deduped 语义；非空则照常取首条返回。
    if not units:
        return {
            "ok": True,
            "op": "add",
            "item_id": None,
            "item": None,
            "skipped": "all derived memories deduped (update/noop)",
        }
    unit = units[0]
    return {"ok": True, "op": "add", "item_id": unit.id, "item": _unit_view(unit)}


def _search(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    # extensions：把调用方在请求里给的自定义配置透传给（可能自定义的）
    # 检索模块。显式校验 dict：extensions 为 truthy 非 dict（字符串/列表等
    # 畸形 JSON）时兜底为空，
    # 避免 .items() 抛 AttributeError → HTTP 500（同 _add 的 metadata 处理）。
    raw_ext = payload.get("extensions")
    if not isinstance(raw_ext, dict):
        raw_ext = {}
    extensions = {k: str(v) for k, v in raw_ext.items()}
    # 自适应披露预算经约定 key 并入 extensions（由 API 边界解析为 typed
    # 预算）。
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        extensions[EXT_MAX_TOKENS] = str(max_tokens)
    trace = bool(payload.get("trace"))
    res = srv.api.recall(
        _require(payload, "query"),
        Context(scope, extensions=extensions),
        identity=actor,
        filters=payload.get("filters"),  # dict DSL / 旧 list：由 API 边界 normalize，非法则 400
        top_k=int(payload.get("k", 10)),
        disclosure=DisclosureLevel.L2,
        with_trajectory=trace,
    )
    hits = [
        {"score": item.score, "item_id": item.unit_id, "content": item.content}
        for item in res.items
    ]
    body = {"ok": True, "op": "search", "hits": hits, "count": len(hits)}
    if trace:
        trajectory = []
        for step in res.trajectory:
            trajectory.append(
                {
                    "stage": step.stage,
                    "channel": step.channel.value if step.channel is not None else None,
                    "candidate_count": step.candidate_count,
                    "cost_ms": round(step.cost_ms, 3),
                    "detail": step.detail,
                }
            )
        body["trajectory"] = trajectory
    return body


def _list(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    offset = _parse_non_negative_int(payload.get("offset"), name="offset", default=0)
    limit = _parse_positive_int(payload.get("limit"), name="limit", default=100)
    memory_types = _parse_string_list(
        payload.get("memory_types", payload.get("mem_types", payload.get("memory_type")))
    )
    if "filters" in payload and "filter" in payload:
        raise ValidationError("filters and filter cannot both be provided")
    filters = payload.get("filters", payload.get("filter"))
    result = srv.api.list(
        scope,
        identity=actor,
        offset=offset,
        limit=limit,
        memory_types=memory_types,
        extensions=_parse_extensions(payload.get("extensions")),
        filters=filters,
    )
    return {
        "ok": True,
        "op": "list",
        "items": [_unit_view(unit) for unit in result.items],
        "count": result.count,
        "offset": offset,
        "limit": limit,
    }


def _get(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    unit = srv.api.get(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "get", "item": _unit_view(unit)}


def _update(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    patch = MemoryPatch(content=payload.get("content"), tags=payload.get("tags"))
    unit = srv.api.update(_require(payload, "item_id"), scope, patch, identity=actor)
    return {"ok": True, "op": "update", "item": _unit_view(unit)}


def _delete(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _identity()
    mode = DeleteMode.PURGE if payload.get("hard") else DeleteMode.FORGET
    selector = DeleteSelector(unit_ids=[_require(payload, "item_id")], scope=scope, mode=mode)
    deleted = srv.api.delete(selector, identity=actor)
    return {"ok": True, "op": "delete", "item_id": payload["item_id"], "deleted": deleted}


# --- 管理面 / 治理 / 演进 verbs ------------------------------------------- #


def _evolve(srv, payload: Body) -> Body:
    """触发演进（extract/associate/consolidate/forget）→ Evolver 全链路 + Scheduler。"""
    scope, actor = _target_scope(payload), _identity()
    mode = EvolveMode(payload.get("mode", "extract"))
    job_id = srv.api.evolve(scope, mode, identity=actor)
    return {"ok": True, "op": "evolve", "mode": mode.value, "job_id": job_id}


def _job(srv, payload: Body) -> Body:
    """查询演进任务状态（Scheduler）。"""
    actor = _identity()
    info = srv.api.job_status(_require(payload, "job_id"), identity=actor)
    return {
        "ok": True,
        "op": "job",
        "job_id": info.id,
        "status": info.status.value,
        "mode": info.mode,
    }


def _inspect(srv, payload: Body) -> Body:
    """治理检视：按 id 读完整单元（含失效版本）→ Governor。"""
    scope, actor = _target_scope(payload), _identity()
    ids = payload.get("item_ids") or [_require(payload, "item_id")]
    units = srv.api.inspect(ids, scope, identity=actor)
    return {"ok": True, "op": "inspect", "items": [_unit_view(u) for u in units]}


def _trace(srv, payload: Body) -> Body:
    """血缘回溯：沿 supersedes 版本链 → Governor。"""
    scope, actor = _target_scope(payload), _identity()
    chain = srv.api.trace(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "trace", "items": [_unit_view(u) for u in chain]}


def _audit(srv, payload: Body) -> Body:
    """审计查询（Governor + AuditLogger）。"""
    actor = _identity()
    filters = {}
    for key in (
        "action",
        "layer",
        "decision",
        "target_id",
        "actor_org",
        "actor_space",
        "actor_user",
        "actor_agent",
        "actor_session",
        "target_org",
        "target_space",
        "target_user",
        "target_agent",
        "target_session",
        "occurred_after",
        "occurred_before",
    ):
        if payload.get(key):
            filters[key] = payload[key]
    events = srv.api.audit(
        filters,
        identity=actor,
        limit=_parse_positive_int(payload.get("limit"), name="limit", default=100),
    )
    return {
        "ok": True,
        "op": "audit",
        "events": [_event_view(e) for e in events],
        "count": len(events),
    }


def _admin(srv, payload: Body) -> Body:
    """运行时策略读写（PolicyManager）：给 value 即 set、给 key 即 get、否则列全部。"""
    actor = _identity()
    key, value = payload.get("key"), payload.get("value")
    if key and value is not None:
        srv.api.admin_set(key, str(value), identity=actor)
        return {
            "ok": True,
            "op": "admin",
            "key": key,
            "value": srv.api.admin_get(key, identity=actor),
        }
    if key:
        return {
            "ok": True,
            "op": "admin",
            "key": key,
            "value": srv.api.admin_get(key, identity=actor),
        }
    return {"ok": True, "op": "admin", "policies": srv.api.admin_all(identity=actor)}


def _grant(srv, payload: Body) -> Body:
    """跨 scope 授权（PermissionManager）。"""
    scope, actor = _target_scope(payload), _identity()
    grantee = Scope(
        org=str(payload.get("grantee_tenant_id", scope.org)) or scope.org,
        space=_space_value(payload, prefix="grantee_") or scope.space,
        user=str(_require(payload, "grantee")),
        agent=str(payload.get("grantee_agent", "")),
        session=str(payload.get("grantee_session", "")),
    )
    grant = Grant(grantor=scope, grantee=grantee, actions=[Action.READ])
    srv.api.grant(grant, identity=actor)
    return {
        "ok": True,
        "op": "grant",
        "grantor": {"space": scope.space, "user": scope.user},
        "grantee": {"space": grantee.space, "user": grantee.user},
    }


def _revoke(srv, payload: Body) -> Body:
    """Cross-scope revoke (PermissionManager)."""
    scope, actor = _target_scope(payload), _identity()
    grantee = Scope(
        org=str(payload.get("grantee_tenant_id", scope.org)) or scope.org,
        space=_space_value(payload, prefix="grantee_") or scope.space,
        user=str(_require(payload, "grantee")),
        agent=str(payload.get("grantee_agent", "")),
        session=str(payload.get("grantee_session", "")),
    )
    grant = Grant(grantor=scope, grantee=grantee, actions=[Action.READ])
    srv.api.revoke(grant, identity=actor)
    return {
        "ok": True,
        "op": "revoke",
        "grantor": {"space": scope.space, "user": scope.user},
        "grantee": {"space": grantee.space, "user": grantee.user},
    }


def _create_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    space = _require_space(payload)
    policy = _space_policy(payload)
    info = srv.api.create_space(
        SpaceSpec(
            org=org,
            space=space,
            display_name=str(payload.get("display_name", "")),
            principal_path=policy.principal_path,
            policy=policy,
            metadata=_string_map(payload.get("metadata")),
        ),
        identity=actor,
    )
    return {"ok": True, "op": "create_space", "space": _space_info_view(info)}


def _get_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    info = srv.api.get_space(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "get_space", "space": _space_info_view(info)}


def _list_spaces(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    raw_status = payload.get("status")
    status = _enum_value(SpaceStatus, raw_status, name="status") if raw_status else None
    spaces = srv.api.list_spaces(
        org,
        identity=actor,
        status=status,
        limit=_parse_positive_int(payload.get("limit"), name="limit", default=100),
        cursor=payload.get("cursor"),
    )
    return {
        "ok": True,
        "op": "list_spaces",
        "spaces": [_space_info_view(info) for info in spaces],
        "count": len(spaces),
    }


def _update_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    patch = SpacePatch(
        display_name=payload.get("display_name"),
        status=_enum_value(SpaceStatus, payload["status"], name="status")
        if payload.get("status")
        else None,
        principal_path=_enum_value(PrincipalPath, payload["principal_path"], name="principal_path")
        if payload.get("principal_path")
        else None,
        policy=_space_policy(payload) if payload.get("policy") else None,
        metadata=_string_map(payload.get("metadata")) if payload.get("metadata") else None,
    )
    info = srv.api.update_space(org, _require_space(payload), patch, identity=actor)
    return {"ok": True, "op": "update_space", "space": _space_info_view(info)}


def _archive_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    info = srv.api.archive_space(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "archive_space", "space": _space_info_view(info)}


def _delete_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    mode = _enum_value(DeleteMode, payload.get("mode", "purge"), name="mode")
    result = srv.api.delete_space(org, _require_space(payload), identity=actor, mode=mode)
    return {
        "ok": True,
        "op": "delete_space",
        "org": result.org,
        "space": result.space,
        "status": result.status.value,
        "deleted_counts": dict(result.deleted_counts),
    }


def _export_space(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    export_id = srv.api.export_space(
        org,
        _require_space(payload),
        identity=actor,
        include_audit=_bool_value(payload.get("include_audit"), default=True),
    )
    return {"ok": True, "op": "export_space", "export_id": export_id}


def _space_usage(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    usage = srv.api.space_usage(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "space_usage", "usage": _usage_view(usage)}


def _get_space_policy(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    policy = srv.api.get_space_policy(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "get_space_policy", "policy": _space_policy_view(policy)}


def _set_space_policy(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    policy = srv.api.set_space_policy(
        org,
        _require_space(payload),
        _space_policy(payload),
        identity=actor,
    )
    return {"ok": True, "op": "set_space_policy", "policy": _space_policy_view(policy)}


def _list_space_members(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    members = srv.api.list_space_members(org, _require_space(payload), identity=actor)
    return {
        "ok": True,
        "op": "list_space_members",
        "members": [_member_view(member) for member in members],
        "count": len(members),
    }


def _add_space_member(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    srv.api.add_space_member(org, _require_space(payload), _space_member(payload), identity=actor)
    return {"ok": True, "op": "add_space_member"}


def _remove_space_member(srv, payload: Body) -> Body:
    actor = _identity()
    org = str(payload.get("tenant_id", "default")) or "default"
    srv.api.remove_space_member(
        org,
        _require_space(payload),
        _member_scope(payload),
        identity=actor,
    )
    return {"ok": True, "op": "remove_space_member"}


_ROUTES: dict[str, Callable[[Any, Body], Body]] = {
    "add": _add,
    "search": _search,
    "list": _list,
    "get": _get,
    "update": _update,
    "delete": _delete,
    "evolve": _evolve,
    "job": _job,
    "inspect": _inspect,
    "trace": _trace,
    "audit": _audit,
    "admin": _admin,
    "grant": _grant,
    "revoke": _revoke,
    "create_space": _create_space,
    "get_space": _get_space,
    "list_spaces": _list_spaces,
    "update_space": _update_space,
    "archive_space": _archive_space,
    "delete_space": _delete_space,
    "export_space": _export_space,
    "space_usage": _space_usage,
    "get_space_policy": _get_space_policy,
    "set_space_policy": _set_space_policy,
    "list_space_members": _list_space_members,
    "add_space_member": _add_space_member,
    "remove_space_member": _remove_space_member,
}


def dispatch(srv, verb: str, payload: Body) -> tuple[int, Body]:
    """Route ``verb`` through the kernel; return ``(status, body)``."""
    handler = _ROUTES.get(verb)
    if handler is None:
        return 404, {"error": "UnknownVerb", "message": f"no such verb: {verb!r}"}
    try:
        # 入口统一拒身份声明，不是每个 verb 各拒一次——单点更难漏。
        _reject_claimed_identity(payload, allow=_AUDIT_FILTER_KEYS if verb == "audit" else ())
        return 200, handler(srv, payload)
    except AgentMemoryError as exc:
        status = next((code for cls, code in _STATUS.items() if isinstance(exc, cls)), 400)
        return status, {"error": type(exc).__name__, "message": str(exc)}
    except Exception as exc:  # surface unexpected failures as 500
        return 500, {"error": "InternalError", "message": str(exc)}
