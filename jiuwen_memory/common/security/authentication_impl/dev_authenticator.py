# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""仅用于隔离开发测试的固定身份或服务端预设身份认证器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any

from jiuwen_memory.common.errors import AuthenticationError, ValidationError
from jiuwen_memory.common.security.authentication.base import Authenticator, AuthProducer
from jiuwen_memory.common.security.types import AuthContext, Credentials, Role
from jiuwen_memory.common.type_def.scope import Scope

_ACTOR_FIELDS = frozenset(field.name for field in fields(Scope))


def _parse_identity(spec: Any) -> AuthContext:
    if not isinstance(spec, Mapping) or set(spec) - {"actor", "role"}:
        raise ValidationError("dev identity must contain only actor and optional role")
    raw_actor = spec.get("actor")
    if not isinstance(raw_actor, Mapping) or set(raw_actor) - _ACTOR_FIELDS:
        raise ValidationError("dev identity actor must be a Scope object")
    for value in raw_actor.values():
        if not isinstance(value, str):
            raise ValidationError("dev identity actor fields must be strings")
    if not raw_actor.get("org", "").strip():
        raise ValidationError("dev identity actor must specify a non-empty org")
    try:
        role = Role(spec.get("role", "user"))
    except (TypeError, ValueError):
        raise ValidationError("dev identity role must be user, admin or root") from None
    return AuthContext(
        actor=Scope(**raw_actor), role=role, credential_type="dev", auth_method="dev"
    )


def _parse_identities(identities: Mapping[str, Any]) -> dict[str, AuthContext]:
    if not isinstance(identities, Mapping) or not identities:
        raise ValidationError("dev identities must be a non-empty object")
    parsed: dict[str, AuthContext] = {}
    for token, spec in identities.items():
        if not isinstance(token, str) or not token:
            raise ValidationError("dev identity selectors must be non-empty strings")
        if any(character.isspace() for character in token):
            raise ValidationError("dev identity selectors must not contain whitespace")
        parsed[token] = _parse_identity(spec)
    return parsed


class DevAuthenticator(Authenticator):
    """无映射时保持固定身份；有映射时凭测试标识选择身份，不得用于生产。"""

    def __init__(
        self, actor: Scope | None = None, *, identities: Mapping[str, Any] | None = None
    ) -> None:
        if actor is not None and identities is not None:
            raise ValidationError("fixed actor and dev identities are mutually exclusive")
        self._identities = None if identities is None else _parse_identities(identities)
        source = actor or Scope(org="local", user="developer")
        self._default_auth = AuthContext(
            actor=replace(source), role=Role.ROOT, credential_type="dev", auth_method="dev"
        )

    def authenticate(self, credentials: Credentials) -> AuthContext:
        """返回请求独享的身份副本；映射模式下未知或缺失标识一律拒绝。"""
        context = self._default_auth
        if self._identities is not None:
            context = self._identities.get(credentials.api_key)
            if context is None:
                raise AuthenticationError("authentication failed")
        return replace(context, actor=replace(context.actor))

    @staticmethod
    def mode() -> str:
        return "dev"

    @staticmethod
    def requires_concurrency_guard() -> bool:
        return False

    @staticmethod
    def health() -> None:
        return None


@AuthProducer.register("dev")
def _build(config) -> DevAuthenticator:
    return DevAuthenticator(identities=config.get("identities"))
