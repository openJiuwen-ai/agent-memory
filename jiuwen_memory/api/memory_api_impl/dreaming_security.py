# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""服务端 dreaming 注册记录的私有安全快照；不接受业务 payload 作为身份来源。"""

from __future__ import annotations

import json
from datetime import datetime

from jiuwen_memory.common.errors import AuthenticationError, PermissionDeniedError
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.types import (
    AuthContext,
    RequestSecurityContext,
    Role,
    Surface,
)
from jiuwen_memory.control.engine_impl.dreaming_registry import actor_from_dict, actor_to_dict

_CONTEXT_KEY = "_request_context_v1"
_TEXT_FIELDS = (
    "credential_type",
    "credential_id",
    "auth_method",
    "credential_issuer",
    "delegation_id",
)
_TIME_FIELDS = ("authenticated_at", "expires_at")


def _registration_identity(security: RequestSecurityContext) -> dict[str, str]:
    """只记录已认证、未篡改上下文；不保存 token、来源证明或延长认证期限。"""
    if not isinstance(security, RequestSecurityContext) or not security.has_valid_origin():
        raise PermissionDeniedError("dreaming requires a trusted security context")
    auth = security.auth
    values = {name: getattr(auth, name) for name in _TEXT_FIELDS}
    values["role"] = auth.role.value
    values["credential_status_required"] = auth.credential_status_required
    for name in _TIME_FIELDS:
        value = getattr(auth, name)
        values[name] = value.isoformat() if value is not None else None
    actor = actor_to_dict(auth.actor)
    actor[_CONTEXT_KEY] = json.dumps(
        {
            "auth": values,
            "surface": security.surface.value,
            "peer": security.peer,
            "attributes": dict(security.attributes),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return actor


def _restored_security(identity: dict[str, str] | None) -> RequestSecurityContext | None:
    """由私有服务端注册表恢复认证事实；每 tick/恢复仍由唯一 PEP 在线复核。"""
    actor = actor_from_dict(identity)
    if actor is None or _CONTEXT_KEY not in identity:
        return None  # 旧记录只有 actor，不能补猜 role、凭据能力或委托。
    if set(identity) != {"org", "space", "user", "agent", "session", _CONTEXT_KEY}:
        raise ValueError("invalid dreaming identity fields")
    for value in identity.values():
        if not isinstance(value, str):
            raise ValueError("invalid dreaming identity field")
    data = json.loads(identity.get(_CONTEXT_KEY))
    if not isinstance(data, dict) or set(data) != {"auth", "surface", "peer", "attributes"}:
        raise ValueError("invalid dreaming security snapshot")
    values = data.get("auth")
    fields = set(_TEXT_FIELDS) | set(_TIME_FIELDS) | {"role", "credential_status_required"}
    if not isinstance(values, dict) or set(values) != fields:
        raise ValueError("invalid dreaming authentication snapshot")
    for name in (*_TEXT_FIELDS, "role"):
        if not isinstance(values.get(name), str):
            raise ValueError("invalid dreaming authentication field")
    if not isinstance(values.get("credential_status_required"), bool):
        raise ValueError("invalid dreaming credential capability")
    for name in _TIME_FIELDS:
        raw = values.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ValueError("invalid dreaming authentication timestamp")
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dreaming authentication timestamp must have timezone")
        values[name] = value
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise ValueError("invalid dreaming environment")
    for key, value in attributes.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("invalid dreaming environment field")
    if not isinstance(data.get("peer"), str):
        raise ValueError("invalid dreaming peer")
    values["role"] = Role(values.get("role"))
    try:
        return new_request_context(
            AuthContext(actor=actor, **values),
            surface=Surface(data.get("surface")),
            peer=data.get("peer"),
            attributes=attributes,
        )
    except AuthenticationError as exc:
        raise ValueError("invalid dreaming actor") from exc
