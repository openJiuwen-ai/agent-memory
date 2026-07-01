"""Verb dispatch — the single code path both the CLI and HTTP surfaces share.

``dispatch(srv, verb, payload) -> (status, body)`` routes a ``(verb, payload)``
to the assembled :class:`~server.Server`'s ``MemoryAPI`` and shapes a JSON-able
envelope the surfaces render. Routing is a table (A20 "route by table"), not an
if/else ladder; domain exceptions map to HTTP-ish status codes.

Scope mapping (DESIGN.md "Two id spaces" / "Mem0 compatibility"): the kernel
scopes by ``tenant_id`` + a single ``scope`` string, mapped onto the native
``Scope(org=tenant_id, user=scope)``. ``actor == target`` here (single-tenant
self-service); a multi-actor surface would pass a distinct caller identity.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from typing import Any, Callable, Dict, Tuple

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"
)
if _SRC not in sys.path:
    sys.path.append(_SRC)

_errors_module = import_module("common.errors")
AgentMemoryError = _errors_module.AgentMemoryError
ConflictError = _errors_module.ConflictError
NotFoundError = _errors_module.NotFoundError
PermissionDeniedError = _errors_module.PermissionDeniedError
PolicyError = _errors_module.PolicyError
ValidationError = _errors_module.ValidationError

_type_def_module = import_module("common.type_def")
AuditEvent = _type_def_module.AuditEvent
Context = _type_def_module.Context
EXT_MAX_TOKENS = _type_def_module.EXT_MAX_TOKENS
MemoryUnit = _type_def_module.MemoryUnit
Modality = _type_def_module.Modality
Scope = _type_def_module.Scope
loads = import_module("common.type_def.memory_codec").loads
EvolveMode = import_module("construction").EvolveMode

_control_types_module = import_module("control.types")
Action = _control_types_module.Action
DeleteMode = _control_types_module.DeleteMode
DeleteSelector = _control_types_module.DeleteSelector
Grant = _control_types_module.Grant
MemoryPatch = _control_types_module.MemoryPatch
DisclosureLevel = import_module("retrieval.types").DisclosureLevel

Body = Dict[str, Any]

_STATUS = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
    ConflictError: 409,
    ValidationError: 400,
    PolicyError: 400,
}


def _scopes(payload: Body) -> Tuple[Scope, Scope]:
    """(target, actor) from tenant_id + scope; actor == target in this surface."""
    scope = Scope(
        org=str(payload.get("tenant_id", "default")) or "default",
        user=str(payload.get("scope", "")),
    )
    return scope, scope


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


def _event_view(ev: AuditEvent) -> Body:
    return {"action": ev.action, "target_id": ev.target_id, "layer": ev.layer}


# --- per-verb handlers ----------------------------------------------------- #


def _add(srv, payload: Body) -> Body:
    scope, actor = _scopes(payload)
    modality = Modality(payload.get("modality", "text"))
    # metadata 透传：infer 等调用级开关经 metadata 下推到引擎（engine.write 从
    # metadata["infer"]=="true" 判定是否同步走 evolve(EXTRACT) 抽取派生记忆）。
    # 值统一 str 化，对齐 RawPayload.metadata 的 Dict[str, str] 约束（同 _search 的 extensions）。
    # 显式校验 dict：metadata 为 truthy 非 dict（字符串/列表等畸形 JSON）时兜底为空，
    # 避免 .items() 抛 AttributeError → HTTP 500。
    raw_meta = payload.get("metadata")
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    metadata = {k: str(v) for k, v in raw_meta.items()}
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
    # （result.created_ids 为空，见 engine.write 的 infer 分支）。此时不伪造 item_id，
    # 如实返回 deduped 语义；非空则照常取首条返回。
    if not units:
        return {"ok": True, "op": "add", "item_id": None, "item": None,
                "skipped": "all derived memories deduped (update/noop)"}
    unit = units[0]
    return {"ok": True, "op": "add", "item_id": unit.id, "item": _unit_view(unit)}


def _search(srv, payload: Body) -> Body:
    scope, actor = _scopes(payload)
    # extensions：把调用方在请求里给的自定义配置透传给（可能自定义的）检索模块。
    # 显式校验 dict：extensions 为 truthy 非 dict（字符串/列表等畸形 JSON）时兜底为空，
    # 避免 .items() 抛 AttributeError → HTTP 500（同 _add 的 metadata 处理）。
    raw_ext = payload.get("extensions")
    if not isinstance(raw_ext, dict):
        raw_ext = {}
    extensions = {k: str(v) for k, v in raw_ext.items()}
    # 自适应披露预算经约定 key 并入 extensions（由 API 边界解析为 typed 预算）。
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        extensions[EXT_MAX_TOKENS] = str(max_tokens)
    trace = bool(payload.get("trace"))  # 入参 trace=true 时附带检索轨迹（默认不返回）
    res = srv.api.recall(
        _require(payload, "query"),
        Context(scope, extensions=extensions),
        identity=actor,
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
        body["trajectory"] = [
            {
                "stage": s.stage,
                "channel": s.channel.value if s.channel is not None else None,
                "candidate_count": s.candidate_count,
                "cost_ms": round(s.cost_ms, 3),
                "detail": s.detail,
            }
            for s in res.trajectory
        ]
    return body


def _list(srv, payload: Body) -> Body:
    scope, _ = _scopes(payload)
    # 真源 KV 同 scope 下除 unit 外还混有索引簿记（如向量侧 /index/chunks/<id>），
    # 这些 loads 不出 unit（返回 None），需跳过，否则 _unit_view(None) 崩。
    items = []
    for _, raw in srv.kv.list(scope):
        unit = loads(raw)
        if unit is not None:
            items.append(_unit_view(unit))
    return {"ok": True, "op": "list", "items": items, "count": len(items)}


def _get(srv, payload: Body) -> Body:
    scope, actor = _scopes(payload)
    unit = srv.api.get(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "get", "item": _unit_view(unit)}


def _update(srv, payload: Body) -> Body:
    scope, actor = _scopes(payload)
    patch = MemoryPatch(content=payload.get("content"), tags=payload.get("tags"))
    unit = srv.api.update(_require(payload, "item_id"), scope, patch, identity=actor)
    return {"ok": True, "op": "update", "item": _unit_view(unit)}


def _delete(srv, payload: Body) -> Body:
    scope, actor = _scopes(payload)
    mode = DeleteMode.PURGE if payload.get("hard") else DeleteMode.FORGET
    selector = DeleteSelector(
        unit_ids=[_require(payload, "item_id")], scope=scope, mode=mode
    )
    deleted = srv.api.delete(selector, identity=actor)
    return {"ok": True, "op": "delete", "item_id": payload["item_id"], "deleted": deleted}


# --- 管理面 / 治理 / 演进 verbs ------------------------------------------- #


def _evolve(srv, payload: Body) -> Body:
    """触发演进（extract/associate/consolidate/forget）→ Evolver 全链路 + Scheduler。"""
    scope, actor = _scopes(payload)
    mode = EvolveMode(payload.get("mode", "extract"))
    job_id = srv.api.evolve(scope, mode, identity=actor)
    return {"ok": True, "op": "evolve", "mode": mode.value, "job_id": job_id}


def _job(srv, payload: Body) -> Body:
    """查询演进任务状态（Scheduler）。"""
    _, actor = _scopes(payload)
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
    scope, actor = _scopes(payload)
    ids = payload.get("item_ids") or [_require(payload, "item_id")]
    units = srv.api.inspect(ids, scope, identity=actor)
    return {"ok": True, "op": "inspect", "items": [_unit_view(u) for u in units]}


def _trace(srv, payload: Body) -> Body:
    """血缘回溯：沿 supersedes 版本链 → Governor。"""
    scope, actor = _scopes(payload)
    chain = srv.api.trace(_require(payload, "item_id"), scope, identity=actor)
    return {"ok": True, "op": "trace", "items": [_unit_view(u) for u in chain]}


def _audit(srv, payload: Body) -> Body:
    """审计查询（Governor + AuditLogger）。"""
    _, actor = _scopes(payload)
    filters = {k: payload[k] for k in ("action", "layer") if payload.get(k)}
    events = srv.api.audit(filters, identity=actor, limit=int(payload.get("limit", 100)))
    return {
        "ok": True,
        "op": "audit",
        "events": [_event_view(e) for e in events],
        "count": len(events),
    }


def _admin(srv, payload: Body) -> Body:
    """运行时策略读写（PolicyManager）：给 value 即 set、给 key 即 get、否则列全部。"""
    _, actor = _scopes(payload)
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
    scope, actor = _scopes(payload)
    grantee = Scope(org=scope.org, user=str(_require(payload, "grantee")))
    grant = Grant(grantor=scope, grantee=grantee, actions=[Action.READ])
    srv.api.grant(grant, identity=actor)
    return {"ok": True, "op": "grant", "grantor": scope.user, "grantee": grantee.user}


_ROUTES: Dict[str, Callable[[Any, Body], Body]] = {
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
}


def dispatch(srv, verb: str, payload: Body) -> Tuple[int, Body]:
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
