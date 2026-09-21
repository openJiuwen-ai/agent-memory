# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit compatibility adapter for non-HTTP flat request callers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.api import RequestSecurityContext, Scope
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
        grantee = (
            Scope(org=target.org, space=target.space, user=str(grantee_raw))
            if isinstance(grantee_raw, str)
            else _scope(payload, "grantee_", target)
        )
    member = None
    if "member" in payload or "member_tenant_id" in payload:
        member_raw = payload.get("member")
        member = (
            Scope(org=target.org, space=target.space, user=str(member_raw))
            if isinstance(member_raw, str)
            else _scope(payload, "member_", target)
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
