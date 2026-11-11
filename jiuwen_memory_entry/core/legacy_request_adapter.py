# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit compatibility adapter for non-HTTP flat request callers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.api import RequestSecurityContext, Scope, ValidationError
from jiuwen_memory_entry.core.dispatch_request import DispatchBatchItem, DispatchRequest

_SCOPE_KEYS = {"tenant_id", "scope", "space", "space_id", "agent", "session"}
# 历史 actor_* 键：不再解释为身份（P1-2——身份只来自 security），但仍是请求信封键，
# 不透传进业务 payload。
_ACTOR_KEYS = {
    "actor_tenant_id",
    "actor_scope",
    "actor_space",
    "actor_space_id",
    "actor_agent",
    "actor_session",
}
_NON_BUSINESS_KEYS = (
    _SCOPE_KEYS
    | _ACTOR_KEYS
    | {
        "grantee",
        "member",
        "target_scope",
        "items",
    }
)
_NON_BUSINESS_PREFIXES = ("grantee_", "member_")


def _space(payload: Mapping[str, Any], prefix: str = "") -> str:
    value = payload.get(f"{prefix}space")
    if value is None:
        value = payload.get(f"{prefix}space_id", "")
    return "" if value is None else str(value)


def _scope(payload: Mapping[str, Any], prefix: str = "", base: Scope | None = None) -> Scope:
    base = base or Scope()
    return Scope(
        org=str(payload.get(f"{prefix}tenant_id") or base.org or "default"),
        space=_space(payload, prefix) or base.space,
        user=str(payload.get(f"{prefix}scope") or base.user),
        agent=str(payload.get(f"{prefix}agent") or base.agent),
        session=str(payload.get(f"{prefix}session") or base.session),
    )


def _nested_scope(payload: Mapping[str, Any], base: Scope) -> Scope:
    """Decode a nested typed Scope while retaining legacy aliases."""

    def _value(name: str, legacy: str, default: str) -> str:
        if name in payload:
            raw = payload[name]
        elif legacy in payload:
            raw = payload[legacy]
        else:
            return default
        return "" if raw is None else str(raw)

    return Scope(
        org=_value("org", "tenant_id", base.org),
        space=_value("space", "space_id", base.space),
        user=_value("user", "scope", base.user),
        agent=_value("agent", "agent", base.agent),
        session=_value("session", "session", base.session),
    )


def _has_identity(raw: Mapping[str, Any]) -> bool:
    """嵌套 scope 对象须携带身份维度：user / scope 别名 / agent 至少一个非空。"""
    # org/space 只是定位维度：{"org": "acme"} 不携带身份，解析后 user 会回落
    # target 自身——那正是本守卫要杜绝的静默自授权。
    return bool(raw.get("user") or raw.get("scope") or raw.get("agent"))


def _business_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    business: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _NON_BUSINESS_KEYS or key.startswith(_NON_BUSINESS_PREFIXES):
            continue
        business[key] = value
    return business


def build_legacy_dispatch_request(
    verb: str,
    payload: Mapping[str, Any],
    *,
    security: RequestSecurityContext,
) -> DispatchRequest:
    """Convert the historical flat surface shape to the structured boundary.

    ``security`` is **required**: a trusted context produced by the composition
    root's authenticator. No actor is derived from the payload—callers without
    a trusted identity source must route through HTTP/API-Key instead.
    """
    target_source = payload
    if verb == "batch_add" and isinstance(payload.get("defaults"), Mapping):
        target_source = payload["defaults"]
    target = _scope(target_source)
    # 业务 target 与可信 actor 分离：target 只描述「操作落在哪」，身份只来自 security。
    actor = security.actor
    grantee = None
    if "grantee" in payload or "grantee_tenant_id" in payload:
        grantee_raw = payload.get("grantee")
        if isinstance(grantee_raw, str):
            grantee = Scope(org=target.org, space=target.space, user=str(grantee_raw))
        elif isinstance(grantee_raw, Mapping):
            if not _has_identity(grantee_raw):
                # 空对象/仅 org·space：user 维度将回落 target 自身（静默自授权）。
                raise ValidationError("'grantee' object must carry a user, scope, or agent field")
            grantee = _nested_scope(grantee_raw, target)
        elif grantee_raw is None and (payload.get("grantee_scope") or payload.get("grantee_agent")):
            # null + 扁平 grantee_* 字段：既有合法形态。判据须为身份维度
            # （scope/agent）非空——仅有 grantee_tenant_id 等定位键时 user
            # 仍会回落 target，静默自授权经由该路径依旧可达。
            grantee = _scope(payload, "grantee_", target)
        else:
            # 数值/列表/布尔等非法类型，或 null 且无身份维度的 grantee_* 字段：
            # 原实现静默兜底成 target 自身，产出 grantor == grantee 的自授权
            # 记录。fail-fast 报 400，由调用方修正后重试。
            raise ValidationError(
                "'grantee' must be a user string, a scope object, or grantee_* fields"
            )
    member = None
    if "member" in payload or "member_tenant_id" in payload:
        member_raw = payload.get("member")
        if isinstance(member_raw, str):
            member = Scope(org=target.org, space=target.space, user=str(member_raw))
        elif isinstance(member_raw, Mapping):
            # 与 grantee 分支对齐：scope 为嵌套 scope 对象时用之解析，标量时
            # 由 _nested_scope 的 legacy 别名（scope→user）解析——
            # {"scope": "alice"} 与 {"user": "alice"} 语义一致，均为合法形态。
            nested = member_raw.get("scope")
            nested = nested if isinstance(nested, Mapping) else member_raw
            if not _has_identity(nested):
                raise ValidationError("'member' object must carry a user, scope, or agent field")
            member = _nested_scope(nested, target)
        elif member_raw is None and (payload.get("member_scope") or payload.get("member_agent")):
            # null + 扁平 member_* 字段：既有合法形态（判据同 grantee 收窄）。
            member = _scope(payload, "member_", target)
        else:
            # 同 grantee：非法类型或 null 且无身份维度的 member_* 字段，拒绝
            # 静默自授权。
            raise ValidationError(
                "'member' must be a user string, a scope object, or member_* fields"
            )

    items: list[DispatchBatchItem] = []
    for item in payload.get("items", ()) if isinstance(payload.get("items"), list) else ():
        if not isinstance(item, Mapping):
            items.append(DispatchBatchItem(target=None, payload={}, legacy_raw_item=item))
            continue
        item_target_raw = item.get("target_scope")
        item_target = (
            _scope(item_target_raw, base=target) if isinstance(item_target_raw, Mapping) else None
        )
        items.append(
            DispatchBatchItem(
                target=item_target,
                payload={key: value for key, value in item.items() if key != "target_scope"},
            )
        )

    business = _business_payload(payload)
    # 嵌套 ``member`` 里的角色与时间是一等字段：``member`` 属非业务键，若不在此
    # 上提，``_space_member`` 只能看到扁平键，成员永远建成最低档且无有效期。
    if verb == "add_space_member" and isinstance(payload.get("member"), Mapping):
        nested_member = payload["member"]
        for key in ("role", "content_role", "governance_role", "created_at", "expires_at"):
            if key in nested_member and key not in business:
                business[key] = nested_member[key]
    return DispatchRequest(
        verb=verb,
        actor=actor,
        target=target,
        security=security,
        payload=business,
        grantee=grantee,
        member=member,
        batch_items=tuple(items),
    )
