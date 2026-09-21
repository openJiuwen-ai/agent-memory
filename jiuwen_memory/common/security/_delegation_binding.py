# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""私有装配：认证凭据绑定委托 ID；资源权限仍由唯一 Authorizer 判定。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

from jiuwen_memory.common.errors import AuthenticationError, PermissionDeniedError, ValidationError
from jiuwen_memory.common.security.authentication.base import Authenticator
from jiuwen_memory.common.security.authentication.key_store import fingerprint
from jiuwen_memory.common.security.authorization.base import Authorizer
from jiuwen_memory.common.security.authorization.scope_rules import scope_covers
from jiuwen_memory.common.security.authorization.store import (
    DelegationStore,
    DelegationStoreProducer,
)
from jiuwen_memory.common.security.types import (
    DELEGATABLE_ACTIONS,
    Action,
    AuthContext,
    Credentials,
    Delegation,
    Role,
)
from jiuwen_memory.common.type_def.scope import Scope

_PROJECTION_PREFIX = "_delegator."


def _stores(authorizer: Authorizer) -> list[DelegationStore]:
    provider = getattr(authorizer, "_delegation_stores", None)
    sources = provider() if callable(provider) else ()
    return list({id(source): source for source in sources}.values())


def _bound_record(
    auth: AuthContext,
    stores: list[DelegationStore],
    *,
    now: datetime,
    action: Action | None = None,
    target: Scope | None = None,
) -> Delegation:
    """仅核实委托记录及绑定；这不是资源授权结论。"""
    records = []
    for store in stores:
        record = store.get(auth.delegation_id)
        if record is not None:
            records.append(record)
    if len(records) != 1:
        raise PermissionDeniedError("delegation_invalid")
    record = records[0]
    actor = auth.actor
    if auth.is_expired(now=now) or auth.role is not Role.USER or not record.is_active(now=now):
        raise PermissionDeniedError("delegation_invalid")
    if not actor.agent or actor.user:
        raise PermissionDeniedError("delegation_invalid")
    if not record.delegate.agent or record.delegate.user:
        raise PermissionDeniedError("delegation_invalid")
    if not scope_covers(record.delegate, actor) or record.delegator.org != actor.org:
        raise PermissionDeniedError("delegation_invalid")
    if record.bound_credential_id and record.bound_credential_id != auth.credential_id:
        raise PermissionDeniedError("delegation_invalid")
    if record.bound_session and record.bound_session != actor.session:
        raise PermissionDeniedError("delegation_invalid")
    if action is not None and not record.permits(action):
        raise PermissionDeniedError("delegation_invalid")
    if target is not None:
        _validate_bound_target(actor, record, target)
    return record


def _validate_bound_target(actor: Scope, record: Delegation, target: Scope) -> None:
    """目标必须同时满足租户、认证主体、委托人和允许空间的收窄约束。"""
    if target.org != actor.org:
        raise PermissionDeniedError("delegation_invalid")
    if actor.space and actor.space != target.space:
        raise PermissionDeniedError("delegation_invalid")
    if record.delegator.space and record.delegator.space != target.space:
        raise PermissionDeniedError("delegation_invalid")
    if record.allowed_spaces and target.space not in record.allowed_spaces:
        raise PermissionDeniedError("delegation_invalid")


def _effective_principal(auth: AuthContext, record: Delegation) -> Scope:
    """资源归属坐标，不是新 actor；保留真实 agent/session 作为作者代理与收窄维。"""
    if not record.delegator.user or record.delegator.agent or record.delegator.session:
        raise PermissionDeniedError("delegation_invalid")
    return Scope(
        org=record.delegator.org,
        user=record.delegator.user,
        agent=auth.actor.agent,
        session=auth.actor.session,
    )


class _DelegationBoundAuthenticator(Authenticator):
    """先委托原认证器认证，再按服务端指纹映射绑定已有委托；不注册公共 target。"""

    def __init__(self, delegate: Authenticator, store: DelegationStore, bindings: dict[str, str]):
        self._delegate = delegate
        self._store = store
        self._bindings = dict(bindings)

    def authenticate(self, credentials: Credentials) -> AuthContext:
        auth = self._delegate.authenticate(credentials)
        credential_id = auth.credential_id or (
            fingerprint(credentials.api_key) if credentials.api_key else ""
        )
        delegation_id = self._bindings.get(credential_id)
        if delegation_id is None:
            return auth
        if auth.delegation_id and auth.delegation_id != delegation_id:
            raise AuthenticationError("authentication failed")
        bound = replace(auth, credential_id=credential_id, delegation_id=delegation_id)
        try:
            record = _bound_record(bound, [self._store], now=datetime.now(timezone.utc))
            _effective_principal(bound, record)
        except PermissionDeniedError:
            raise AuthenticationError("authentication failed") from None
        return bound

    def mode(self) -> str:
        return self._delegate.mode()

    def requires_loopback_binding(self) -> bool:
        return self._delegate.requires_loopback_binding()

    def requires_concurrency_guard(self) -> bool:
        return self._delegate.requires_concurrency_guard()

    def bind_instance_name(self, name: str) -> None:
        self._delegate.bind_instance_name(name)

    def _credential_sources(self):
        provider = getattr(self._delegate, "_credential_sources", None)
        return provider() if callable(provider) else ()

    def health(self) -> None:
        self._delegate.health()
        self._store.health()

    def close(self) -> None:
        closer = getattr(self._delegate, "close", None)
        if callable(closer):
            closer()


def _parse_record(raw: object) -> Delegation:
    """仅处理可信部署配置；绝不从 HTTP payload/header 建立委托记录。"""
    try:
        if not isinstance(raw, Mapping):
            raise ValueError
        values = dict(raw)
        for key in ("delegator", "delegate"):
            scope = values[key]
            if not isinstance(scope, Mapping) or any(
                not isinstance(v, str) for v in scope.values()
            ):
                raise ValueError
            values[key] = Scope(**scope)
        for key in ("expires_at", "not_before"):
            value = values.get(key)
            if value is None and key == "not_before":
                continue
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(parsed, datetime) or parsed.tzinfo is None:
                raise ValueError
            values[key] = parsed
        actions = values["actions"]
        if not isinstance(actions, list) or not actions:
            raise ValueError
        values["actions"] = frozenset(Action(action) for action in actions)
        if not values["actions"] <= DELEGATABLE_ACTIONS:
            raise ValueError
        spaces = values.get("allowed_spaces", [])
        if not isinstance(spaces, list) or any(not isinstance(s, str) or not s for s in spaces):
            raise ValueError
        values["allowed_spaces"] = frozenset(spaces)
        record = Delegation(**values)
        _validate_record_fields(record)
        return record
    except (KeyError, TypeError, ValueError):
        raise ValidationError("invalid server-side delegation configuration") from None


def _validate_record_fields(record: Delegation) -> None:
    """按标识、双方主体和绑定字段分别校验，不隐式转换配置类型。"""
    if not isinstance(record.delegation_id, str) or not record.delegation_id:
        raise ValueError
    if not record.delegator.org or not record.delegator.user:
        raise ValueError
    if record.delegator.agent or record.delegator.session:
        raise ValueError
    if not record.delegate.agent or record.delegate.user:
        raise ValueError
    if record.delegate.org != record.delegator.org:
        raise ValueError
    if not isinstance(record.revoked, bool):
        raise ValueError
    if not isinstance(record.bound_credential_id, str) or not isinstance(record.bound_session, str):
        raise ValueError


def _bind_delegations(
    authenticator: Authenticator, authorizer: Authorizer, config
) -> Authenticator:
    bindings = config.get("delegation_bindings")
    records = config.get("delegations", [])
    if bindings is None and not records:
        return authenticator
    if not isinstance(bindings, Mapping) or not bindings or not isinstance(records, list):
        raise ValidationError("delegation_bindings must map credential fingerprints to record IDs")
    for key, value in bindings.items():
        if not isinstance(key, str) or not key:
            raise ValidationError("delegation_bindings keys must be nonempty fingerprints")
        if not isinstance(value, str) or not value:
            raise ValidationError("delegation_bindings values must be nonempty record IDs")
    store_name = config.get("delegation_store", "default")
    if not isinstance(store_name, str) or not store_name:
        raise ValidationError("delegation_store must reference a named store")
    store = DelegationStoreProducer.build_named(store_name, config.ctx)
    if not any(store is source for source in _stores(authorizer)):
        raise ValidationError(
            "delegation binding and Authorizer must share the same DelegationStore"
        )
    parsed = [_parse_record(raw) for raw in records]
    if len({r.delegation_id for r in parsed}) != len(parsed):
        raise ValidationError("duplicate delegation ID in server configuration")
    for record in parsed:
        store.add(record)
    return _DelegationBoundAuthenticator(authenticator, store, dict(bindings))
