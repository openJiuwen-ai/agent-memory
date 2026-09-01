# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit compatibility adapter for non-HTTP flat request callers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwen_memory.common.security.types import Surface
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory_entry.core.dispatch_request import DispatchBatchItem, DispatchRequest

_SCOPE_KEYS = {"tenant_id", "scope", "space", "space_id", "agent", "session"}
_ACTOR_KEYS = {
    "actor_tenant_id",
    "actor_scope",
    "actor_space",
    "actor_space_id",
    "actor_agent",
    "actor_session",
}
_NON_BUSINESS_KEYS = _SCOPE_KEYS | _ACTOR_KEYS | {
    "grantee",
    "member",
    "target_scope",
    "items",
}
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


def _actor(payload: Mapping[str, Any], target: Scope) -> Scope:
    if not any(key in payload for key in _ACTOR_KEYS):
        return target
    actor_space = (
        _space(payload, "actor_")
        if "actor_space" in payload or "actor_space_id" in payload
        else target.space
    )
    return Scope(
        org=str(payload.get("actor_tenant_id") or target.org or "default"),
        space=actor_space,
        user=str(payload.get("actor_scope", "")),
        agent=str(payload.get("actor_agent", "")),
        session=str(payload.get("actor_session", "")),
    )


def _business_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    business: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _NON_BUSINESS_KEYS or key.startswith(_NON_BUSINESS_PREFIXES):
            continue
        business[key] = value
    return business


def build_legacy_dispatch_request(
    verb: str, payload: Mapping[str, Any], *, surface: Surface = Surface.INTERNAL
) -> DispatchRequest:
    """Convert the historical flat surface shape to the structured boundary."""
    target_source = payload
    if verb == "batch_add" and isinstance(payload.get("defaults"), Mapping):
        target_source = payload["defaults"]
    target = _scope(target_source)
    actor_source = dict(target_source)
    actor_source.update({key: value for key, value in payload.items() if key in _ACTOR_KEYS})
    actor = _actor(actor_source, target)
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
        payload=business,
        surface=surface,
        grantee=grantee,
        member=member,
        batch_items=tuple(items),
    )
