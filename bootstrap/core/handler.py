# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verb dispatch — the single code path both the CLI and HTTP surfaces share.

``dispatch(srv, verb, payload) -> (status, body)`` routes a ``(verb, payload)``
to the assembled :class:`~server.Server`'s ``MemoryAPI`` and shapes a JSON-able
envelope the surfaces render. Routing is a table (A20 "route by table"), not an
if/else ladder; domain exceptions map to HTTP-ish status codes.

Scope mapping (DESIGN.md "Two id spaces" / "Mem0 compatibility"): the kernel
scopes by ``tenant_id`` + optional ``space`` / ``space_id`` + a single
``scope`` string, mapped onto the native
``Scope(org=tenant_id, space=space, user=scope)``. The request shape keeps old
empty-space payloads compatible, while allowing an optional claimed actor
override via ``actor_tenant_id`` / ``actor_space`` / ``actor_scope`` fields.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from importlib import import_module
from typing import Any, Callable

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
if _SRC not in sys.path:
    # 仓库根兜底，供 ``jiuwen_memory.*`` 解析（scripts 已设 PYTHONPATH 时为幂等）。
    sys.path.append(_SRC)

_errors_module = import_module("jiuwen_memory.common.errors")
AgentMemoryError = _errors_module.AgentMemoryError
ConflictError = _errors_module.ConflictError
NotFoundError = _errors_module.NotFoundError
PermissionDeniedError = _errors_module.PermissionDeniedError
PolicyError = _errors_module.PolicyError
ValidationError = _errors_module.ValidationError

_type_def_module = import_module("jiuwen_memory.common.type_def")
AuditEvent = _type_def_module.AuditEvent
Context = _type_def_module.Context
EXT_MAX_TOKENS = _type_def_module.EXT_MAX_TOKENS
MEMORY_KEY_PREFIX = _type_def_module.MEMORY_KEY_PREFIX
MemoryUnit = _type_def_module.MemoryUnit
Modality = _type_def_module.Modality
Scope = _type_def_module.Scope
EvolveMode = import_module("jiuwen_memory.construction").EvolveMode

_control_types_module = import_module("jiuwen_memory.control.types")
Action = _control_types_module.Action
DeleteMode = _control_types_module.DeleteMode
DeleteSelector = _control_types_module.DeleteSelector
Grant = _control_types_module.Grant
MemoryPatch = _control_types_module.MemoryPatch
BatchWriteItem = _control_types_module.BatchWriteItem
PrincipalPath = _control_types_module.PrincipalPath
SpaceMember = _control_types_module.SpaceMember
SpacePatch = _control_types_module.SpacePatch
SpacePolicy = _control_types_module.SpacePolicy
SpaceSpec = _control_types_module.SpaceSpec
SpaceStatus = _control_types_module.SpaceStatus
DisclosureLevel = import_module("jiuwen_memory.retrieval.types").DisclosureLevel

Body = dict[str, Any]

_STATUS = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
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


def _scope_from_payload(payload: Body, base: Scope | None = None) -> Scope:
    """Parse an optional batch item scope override over a default target scope."""
    base = base or Scope()
    return Scope(
        org=str(payload.get("tenant_id") or base.org or "default"),
        space=_space_value(payload) if "space" in payload or "space_id" in payload else base.space,
        user=str(payload.get("scope", base.user)),
        agent=str(payload.get("agent", base.agent)),
        session=str(payload.get("session", base.session)),
    )


def _actor_scope(payload: Body) -> Scope:
    """Claimed actor scope; defaults to payload scope, with optional explicit override."""
    has_actor_override = False
    actor_fields = (
        "actor_tenant_id",
        "actor_space",
        "actor_space_id",
        "actor_scope",
        "actor_agent",
        "actor_session",
    )
    for key in actor_fields:
        if key in payload:
            has_actor_override = True
            break

    if has_actor_override:
        actor_org = str(payload.get("actor_tenant_id", ""))
        if actor_org == "":
            actor_org = str(payload.get("tenant_id", "default")) or "default"
        actor_space = (
            _space_value(payload, prefix="actor_")
            if "actor_space" in payload or "actor_space_id" in payload
            else _space_value(payload)
        )
        return Scope(
            org=actor_org,
            space=actor_space,
            user=str(payload.get("actor_scope", "")),
            agent=str(payload.get("actor_agent", "")),
            session=str(payload.get("actor_session", "")),
        )
    return Scope(
        org=str(payload.get("tenant_id", "default")) or "default",
        space=_space_value(payload),
        user=str(payload.get("scope", "")),
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
        "system_metadata": dict(unit.system_metadata),
        "user_metadata": dict(unit.user_metadata),
    }


def _batch_item_view(item: BatchWriteItem) -> Body:
    return {
        "content": item.content,
        "scope": {
            "tenant_id": item.scope.org if item.scope else "",
            "space": item.scope.space if item.scope else "",
            "scope": item.scope.user if item.scope else "",
            "agent": item.scope.agent if item.scope else "",
            "session": item.scope.session if item.scope else "",
        },
        "source": item.source.value if isinstance(item.source, Modality) else item.source,
        "assets": item.assets,
        "tags": item.tags,
        "system_metadata": item.system_metadata,
        "user_metadata": item.user_metadata,
        "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
        "stream_id": item.stream_id,
        "sequence": item.sequence,
        "idempotency_key": item.idempotency_key,
    }


def _parse_occurred_at(value: Any, *, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO 8601 datetime")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{name} must be an ISO 8601 datetime") from None


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
        storage_isolation_strategy=str(
            raw.get("storage_isolation_strategy", "metadata_filter")
        ),
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


def _video_unit_view(unit: MemoryUnit) -> Body:
    """Serialize fields needed by video job responses."""
    return {
        **_unit_view(unit),
        "source": unit.source.value,
        "source_ref": unit.source_ref,
        "provenance": list(unit.provenance),
    }


def _ensure_video_chain(srv) -> None:
    """Fail-closed: reject video ingest when the multimodal chain is not assembled."""
    memory_api = srv.config.settings.get("memory_api")
    if not isinstance(memory_api, dict):
        raise ValidationError("video ingest requires multimodal configuration")
    normalizer_cfg = memory_api.get("normalizer")
    default_normalizer = (
        normalizer_cfg.get("default", {}) if isinstance(normalizer_cfg, dict) else {}
    )
    default_params = (
        default_normalizer.get("params", {})
        if isinstance(default_normalizer, dict)
        else {}
    )
    routes = default_params.get("routes") if isinstance(default_params, dict) else None
    has_video_normalizer = isinstance(routes, dict) and "video" in routes
    evolver_cfg = memory_api.get("evolver")
    has_video_evolver = isinstance(evolver_cfg, dict) and "video" in evolver_cfg
    if not (has_video_normalizer and has_video_evolver):
        raise ValidationError(
            "video ingest requires routing normalizer with 'video' route and video evolver"
        )


def _submit_video(srv, payload: Body, *, scope: Scope, identity: Scope) -> Body:
    """Submit a video write through the Control-managed ingest queue."""
    uri = str(_require(payload, "uri")).strip()
    if not uri:
        raise ValidationError("missing required field: 'uri'")
    _ensure_video_chain(srv)
    payload_id = str(payload.get("payload_id") or uuid.uuid4())
    raw_system_metadata = payload.get("system_metadata")
    raw_user_metadata = payload.get("user_metadata")
    if raw_system_metadata is not None and not isinstance(raw_system_metadata, dict):
        raise ValidationError("system_metadata must be an object")
    if raw_user_metadata is not None and not isinstance(raw_user_metadata, dict):
        raise ValidationError("user_metadata must be an object")
    system_metadata = dict(raw_system_metadata or {})
    user_metadata = dict(raw_user_metadata or {})
    system_metadata.update(
        {"infer": "true", "pipeline": "video", "payload_id": payload_id}
    )
    requested_assets = payload.get("assets")
    extras = requested_assets if isinstance(requested_assets, list) else []
    assets = [uri, *(str(item) for item in extras if str(item) != uri)]

    # P1-2: 提交前完成 WRITE 鉴权 + 输入合法性校验，后台 add() 仍保留一次鉴权作防御层。
    # 无权限请求在此被拒（PermissionDeniedError → 403），不进队列，避免占满 worker。
    srv.api.check_write(
        scope,
        identity,
        tags=payload.get("tags"),
        system_metadata=system_metadata,
        user_metadata=user_metadata,
    )

    submission = srv.ingest_jobs.submit(
        payload_id=payload_id,
        source_ref=uri,
        scope=scope,
        task=lambda: srv.api.add(
            uri,
            scope,
            Modality.VIDEO,
            identity=identity,
            assets=assets,
            tags=payload.get("tags"),
            system_metadata=system_metadata,
            user_metadata=user_metadata,
        ),
    )
    job = submission.job
    return {
        "ok": True,
        "op": "add",
        "accepted": True,
        "job_id": job.id,
        "video_id": payload_id,
        "status": job.status,
        "reused": submission.reused,
        "feedback_message": (
            "已返回该视频现有的处理任务。"
            if submission.reused
            else "视频处理任务已提交。"
        ),
    }


def _ingest_job_status(srv, job, *, identity: Scope) -> Body:
    """Adapt a Control ingest job to the shared job response shape."""
    scope = job.scope
    units = []
    unit_ids = [item for item in job.detail.get("unit_ids", "").split(",") if item]
    for unit_id in unit_ids:
        try:
            units.append(srv.api.get(unit_id, scope, identity=identity))
        except NotFoundError:
            continue
    items = [_video_unit_view(unit) for unit in units]
    body: Body = {
        "ok": True,
        "op": "job",
        "job_id": job.id,
        "video_id": job.detail.get("payload_id", ""),
        "status": job.status.value,
        "count": len(items),
        "item_ids": [item["item_id"] for item in items],
        "items": items,
    }
    if job.status.value == "succeeded":
        body["feedback_message"] = f"视频处理完成，共生成 {len(items)} 条多模态记忆。"
    elif job.status.value == "failed":
        body["error"] = job.detail.get("error", "")
        body["feedback_message"] = "视频处理失败。"
    else:
        body["feedback_message"] = "视频正在处理中。"
    return body


# --- per-verb handlers ----------------------------------------------------- #


def _add(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _actor_scope(payload)
    if "metadata" in payload:
        raise ValidationError(
            "metadata has been removed; use system_metadata and user_metadata"
        )
    modality = Modality(payload.get("modality", "text"))
    if modality == Modality.VIDEO:
        return _submit_video(srv, payload, scope=scope, identity=actor)
    # metadata 透传：infer 等调用级开关经 metadata 下推到引擎（engine.write 从
    # metadata["infer"]=="true" 判定是否同步走 evolve(EXTRACT) 抽取派生记忆）。
    # JSON 标量原样透传（不 str 化）：数值/布尔要保持原生类型才能在索引里建
    # double/boolean mapping 并原生下推；合法性由 API 写入边界统一校验。
    # 显式校验 dict：metadata 为 truthy 非 dict（字符串/列表等畸形 JSON）时兜底为空，
    # 避免下游 .items() 抛 AttributeError → HTTP 500。
    raw_system_metadata = payload.get("system_metadata")
    raw_user_metadata = payload.get("user_metadata")
    if raw_system_metadata is not None and not isinstance(raw_system_metadata, dict):
        raise ValidationError("system_metadata must be an object")
    if raw_user_metadata is not None and not isinstance(raw_user_metadata, dict):
        raise ValidationError("user_metadata must be an object")
    units = srv.api.add(
        _require(payload, "content"),
        scope,
        modality,
        identity=actor,
        tags=payload.get("tags"),
        assets=payload.get("assets"),
        system_metadata=dict(raw_system_metadata or {}) or None,
        user_metadata=dict(raw_user_metadata or {}) or None,
    )
    # infer=True 时引擎可能合法返回空：派生记忆全部被 dedup 判为 update/noop
    # （result.created_ids 为空，见 engine.write 的 infer 分支）。
    # 此时不伪造 item_id，
    # 如实返回 deduped 语义；非空则照常取首条返回。
    if not units:
        return {"ok": True, "op": "add", "item_id": None, "item": None,
                "skipped": "all derived memories deduped (update/noop)"}
    unit = units[0]
    return {"ok": True, "op": "add", "item_id": unit.id, "item": _unit_view(unit)}


def _batch_add(srv, payload: Body) -> Body:
    raw_defaults = payload.get("defaults", {})
    if not isinstance(raw_defaults, dict):
        raise ValidationError("batch_add defaults must be an object")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationError("batch_add items must be a non-empty list")
    if not isinstance(payload.get("continue_on_error", True), bool):
        raise ValidationError("batch_add continue_on_error must be boolean")

    defaults = {key: value for key, value in payload.items() if key not in {"defaults", "items"}}
    defaults.update(raw_defaults)
    if "metadata" in defaults:
        raise ValidationError(
            "batch_add metadata has been removed; use system_metadata and user_metadata"
        )
    default_scope = _scope_from_payload(defaults)
    raw_system_metadata = defaults.get("system_metadata")
    raw_user_metadata = defaults.get("user_metadata")
    if raw_system_metadata is not None and not isinstance(raw_system_metadata, dict):
        raise ValidationError("batch_add defaults.system_metadata must be an object")
    if raw_user_metadata is not None and not isinstance(raw_user_metadata, dict):
        raise ValidationError("batch_add defaults.user_metadata must be an object")
    default_tags = defaults.get("tags")
    if default_tags is not None and not isinstance(default_tags, list):
        raise ValidationError("batch_add defaults.tags must be an array")
    try:
        default_source = Modality(defaults.get("source", defaults.get("modality", "text")))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid batch_add default source: {exc}") from exc
    default_occurred_at = _parse_occurred_at(
        defaults.get("occurred_at"), name="batch_add defaults.occurred_at"
    )

    items: list[BatchWriteItem | object] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            items.append(raw_item)
            continue
        if "metadata" in raw_item:
            raise ValidationError(
                "batch item metadata has been removed; use system_metadata and user_metadata"
            )
        raw_scope = raw_item.get("target_scope", {})
        if not isinstance(raw_scope, dict):
            items.append(raw_item)
            continue
        item_scope = _scope_from_payload(raw_scope, default_scope)
        raw_source = raw_item.get("source", raw_item.get("modality"))
        try:
            item_source = Modality(raw_source) if raw_source is not None else None
        except (TypeError, ValueError):
            items.append(raw_item)
            continue
        try:
            item_occurred_at = _parse_occurred_at(
                raw_item.get("occurred_at"), name="batch_add item occurred_at"
            )
        except ValidationError:
            items.append(raw_item)
            continue
        items.append(
            BatchWriteItem(
                content=raw_item.get("content"),
                scope=item_scope,
                source=item_source,
                assets=raw_item.get("assets"),
                tags=raw_item.get("tags"),
                system_metadata=raw_item.get("system_metadata"),
                user_metadata=raw_item.get("user_metadata"),
                occurred_at=item_occurred_at,
                stream_id=raw_item.get("stream_id", ""),
                sequence=raw_item.get("sequence"),
                idempotency_key=raw_item.get("idempotency_key", ""),
            )
        )

    result = srv.api.batch_add(
        items,
        default_scope,
        default_source,
        identity=_actor_scope(defaults),
        tags=default_tags,
        system_metadata=raw_system_metadata,
        user_metadata=raw_user_metadata,
        occurred_at=default_occurred_at,
        stream_id=defaults.get("stream_id", ""),
        continue_on_error=payload.get("continue_on_error", True),
    )
    outcomes = []
    for raw_item, outcome in zip(raw_items, result.outcomes):
        outcomes.append(
            {
                "index": outcome.index,
                "input": raw_item,
                "item": _batch_item_view(outcome.item),
                "items": [_unit_view(unit) for unit in outcome.units],
                "ok": not outcome.error,
                "error": outcome.error,
                "error_type": outcome.error_type,
            }
        )
    return {
        "ok": all(outcome["ok"] for outcome in outcomes),
        "op": "batch_add",
        "outcomes": outcomes,
    }


def _search(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _actor_scope(payload)
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
    res = srv.api.search(
        _require(payload, "query"),
        Context(scope, extensions=extensions),
        identity=actor,
        filters=payload.get("filters"),  # dict DSL / 旧 list：由 API 边界 normalize，非法则 400
        top_k=int(payload.get("k", 10)),
        disclosure=DisclosureLevel.L2,
        with_trajectory=trace,
    )
    hits = [
        {
            "score": item.score,
            "item_id": item.unit_id,
            "content": item.content,
            "user_metadata": dict(item.user_metadata),
        }
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
    scope, actor = _target_scope(payload), _actor_scope(payload)
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
    scope, actor = _target_scope(payload), _actor_scope(payload)
    unit = srv.api.get(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "get", "item": _unit_view(unit)}


def _update(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _actor_scope(payload)
    if "metadata" in payload:
        raise ValidationError(
            "metadata has been removed; use system_metadata and user_metadata"
        )
    patch = MemoryPatch(
        content=payload.get("content"),
        tags=payload.get("tags"),
        system_metadata=payload.get("system_metadata"),
        user_metadata=payload.get("user_metadata"),
    )
    unit = srv.api.update(_require(payload, "item_id"), scope, patch, identity=actor)
    return {"ok": True, "op": "update", "item": _unit_view(unit)}


def _delete(srv, payload: Body) -> Body:
    scope, actor = _target_scope(payload), _actor_scope(payload)
    mode = DeleteMode.PURGE if payload.get("hard") else DeleteMode.FORGET
    selector = DeleteSelector(
        unit_ids=[_require(payload, "item_id")], scope=scope, mode=mode
    )
    deleted = srv.api.delete(selector, identity=actor)
    return {"ok": True, "op": "delete", "item_id": payload["item_id"], "deleted": deleted}


# --- 管理面 / 治理 / 演进 verbs ------------------------------------------- #


def _evolve(srv, payload: Body) -> Body:
    """触发演进（extract/associate/consolidate/forget）→ Evolver 全链路 + Scheduler。"""
    scope, actor = _target_scope(payload), _actor_scope(payload)
    mode = EvolveMode(payload.get("mode", "extract"))
    job_id = srv.api.evolve(scope, mode, identity=actor)
    return {"ok": True, "op": "evolve", "mode": mode.value, "job_id": job_id}


def _job(srv, payload: Body) -> Body:
    """查询视频 Ingest 任务或原生 Scheduler 任务状态。"""
    job_id = str(_require(payload, "job_id"))
    actor = _actor_scope(payload)
    info = srv.api.job_status(
        job_id,
        identity=actor,
        scope=_target_scope(payload),
    )
    if info.mode == "ingest":
        return _ingest_job_status(srv, info, identity=actor)
    return {
        "ok": True,
        "op": "job",
        "job_id": info.id,
        "status": info.status.value,
        "mode": info.mode,
    }


def _inspect(srv, payload: Body) -> Body:
    """治理检视：按 id 读完整单元（含失效版本）→ Governor。"""
    scope, actor = _target_scope(payload), _actor_scope(payload)
    ids = payload.get("item_ids") or [_require(payload, "item_id")]
    units = srv.api.inspect(ids, scope, identity=actor)
    return {"ok": True, "op": "inspect", "items": [_unit_view(u) for u in units]}


def _trace(srv, payload: Body) -> Body:
    """血缘回溯：沿 supersedes 版本链 → Governor。"""
    scope, actor = _target_scope(payload), _actor_scope(payload)
    chain = srv.api.trace(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "trace", "items": [_unit_view(u) for u in chain]}


def _audit(srv, payload: Body) -> Body:
    """审计查询（Governor + AuditLogger）。"""
    actor = _actor_scope(payload)
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
    """运行时策略读写：给 value 即 set、给 key 即 get、否则列全部。"""
    actor = _actor_scope(payload)
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
    scope, actor = _target_scope(payload), _actor_scope(payload)
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
    scope, actor = _target_scope(payload), _actor_scope(payload)
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
    actor = _actor_scope(payload)
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
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    info = srv.api.get_space(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "get_space", "space": _space_info_view(info)}


def _list_spaces(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
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
    actor = _actor_scope(payload)
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
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    info = srv.api.archive_space(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "archive_space", "space": _space_info_view(info)}


def _delete_space(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
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
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    export_id = srv.api.export_space(
        org,
        _require_space(payload),
        identity=actor,
        include_audit=_bool_value(payload.get("include_audit"), default=True),
    )
    return {"ok": True, "op": "export_space", "export_id": export_id}


def _space_usage(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    usage = srv.api.space_usage(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "space_usage", "usage": _usage_view(usage)}


def _get_space_policy(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    policy = srv.api.get_space_policy(org, _require_space(payload), identity=actor)
    return {"ok": True, "op": "get_space_policy", "policy": _space_policy_view(policy)}


def _set_space_policy(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    policy = srv.api.set_space_policy(
        org,
        _require_space(payload),
        _space_policy(payload),
        identity=actor,
    )
    return {"ok": True, "op": "set_space_policy", "policy": _space_policy_view(policy)}


def _list_space_members(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    members = srv.api.list_space_members(org, _require_space(payload), identity=actor)
    return {
        "ok": True,
        "op": "list_space_members",
        "members": [_member_view(member) for member in members],
        "count": len(members),
    }


def _add_space_member(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
    org = str(payload.get("tenant_id", "default")) or "default"
    srv.api.add_space_member(org, _require_space(payload), _space_member(payload), identity=actor)
    return {"ok": True, "op": "add_space_member"}


def _remove_space_member(srv, payload: Body) -> Body:
    actor = _actor_scope(payload)
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
    "batch_add": _batch_add,
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
        return 200, handler(srv, payload)
    except AgentMemoryError as exc:
        status = next(
            (code for cls, code in _STATUS.items() if isinstance(exc, cls)), 400
        )
        return status, {"error": type(exc).__name__, "message": str(exc)}
    except Exception as exc:  # surface unexpected failures as 500
        return 500, {"error": "InternalError", "message": str(exc)}
