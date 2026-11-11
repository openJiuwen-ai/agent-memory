# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authenticated explicit memory import/clear tool, separate from runtime reads."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from jiuwen_memory.common.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.security.authentication.credential_registry import (
    CredentialStatusRegistry,
)
from jiuwen_memory.common.security.authorization.scope_rules import migration_scope_allowed
from jiuwen_memory.common.security.capabilities import credential_sources
from jiuwen_memory.common.security.types import (
    Action,
    AuthorizationEnvironment,
    Credentials,
    ResourceDescriptor,
    Role,
)
from jiuwen_memory.common.type_def import AuditEvent, Scope
from jiuwen_memory.config import Config
from jiuwen_memory.storage.kv import MigrationVerificationError
from jiuwen_memory.storage.store_manager import StorageCapability

from .validation import FormatUpgradeRequired, validate_plaintext_memory


@dataclass(frozen=True)
class MigrationItemResult:
    index: int
    status: str
    error: str = ""
    source: str = ""
    committed: bool = False


@dataclass(frozen=True)
class MigrationResult:
    operation: str
    status: str
    items: tuple[MigrationItemResult, ...] = ()
    error: str = ""
    cursor: str = ""


@dataclass(frozen=True)
class _Candidate:
    scope: Scope
    key: str
    value: bytes | None


def _scope_key(scope):
    return (scope.org, scope.space, scope.user, scope.agent, scope.session)


def _resume_cursor(cursor, identity, operation):
    if not cursor:
        return None
    try:
        if not isinstance(cursor, str) or len(cursor) > 65536:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (data["v"], data["inventory"], data["operation"]) != (1, identity, operation):
            raise ValueError
        if not isinstance(data["source"], str) or not isinstance(data["after"], str):
            raise ValueError
        if not isinstance(data["scope"], list) or len(data["scope"]) != 5:
            raise ValueError
        if any(not isinstance(value, str) for value in data["scope"]):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError):
        raise ValidationError("invalid migration cursor or changed storage inventory") from None


def _next_cursor(identity, source, scope, after, operation):
    return base64.urlsafe_b64encode(json.dumps({
        "v": 1, "inventory": identity, "source": source,
        "scope": _scope_key(scope), "after": after,
        "operation": operation,
    }, separators=(",", ":")).encode()).decode()


def _allowed(session, security, scope, record_id="") -> bool:
    auth = security.auth
    registry = CredentialStatusRegistry()
    for kind, issuer, store in credential_sources(session.security_runtime.authenticator):
        registry.register(kind, issuer, store)
    if auth.is_expired() or registry.is_revoked(auth):
        raise AuthenticationError("authentication failed")
    if auth.delegation_id or not migration_scope_allowed(auth, scope):
        return False
    return session.security_runtime.authorizer.authorize(
        auth=auth,
        resource=ResourceDescriptor(
            action=Action.MIGRATE_MEMORY,
            resource_type="memory_unit",
            scope=scope,
            resource_id=record_id,
        ),
        environment=AuthorizationEnvironment(now=datetime.now(UTC)),
    ).allowed


def _is_target(target) -> bool:
    # Authorized port proxies delegate public capabilities dynamically; Python 3.12's
    # runtime Protocol check uses getattr_static and would incorrectly reject them.
    methods = (
        "migration_scopes",
        "migration_records",
        "migration_identity",
        "migration_key",
        "migration_validate_scope",
        "migrate_plaintext",
    )
    if not target.security.enabled():
        return False
    return all(callable(getattr(target, name, None)) for name in methods)


def _code(exc: Exception) -> str:
    for error_type, code in (
        (MigrationVerificationError, "verification_failed"),
        (FormatUpgradeRequired, "format_upgrade_required"),
        (AuthenticationError, "authentication_failed"),
        (PermissionDeniedError, "permission_denied"),
        (ValidationError, "invalid_input"),
        (ConflictError, "target_conflict"),
        (NotFoundError, "not_found"),
        (UnsupportedCapabilityError, "unsupported"),
    ):
        if isinstance(exc, error_type):
            return code
    return "operation_failed"


def _process(session, security, records, *, operation, port, limit, target=None):
    registry = CredentialStatusRegistry()
    for kind, issuer, store in credential_sources(session.security_runtime.authenticator):
        registry.register(kind, issuer, store)
    if target is None:
        target = session.storage.kv(port)
    if not _is_target(target):
        raise UnsupportedCapabilityError("plaintext_migration", port, "storage")
    results = []
    iterator = iter(records)
    index = 0
    while True:
        try:
            entry = next(iterator)
        except StopIteration:
            break
        except Exception:
            results.append(MigrationItemResult(index + 1, "failed", "input_failed"))
            break
        index += 1
        if index > 10000:
            results.append(MigrationItemResult(index, "failed", "batch_limit"))
            break
        committed = False
        try:
            if isinstance(entry, _Candidate) and entry.value is None:
                results.append(MigrationItemResult(index, "failed", "record_too_large"))
                continue
            raw = entry.value if isinstance(entry, _Candidate) else entry
            unit = validate_plaintext_memory(raw, max_bytes=limit)
            target.migration_validate_scope(unit.scope)
            if isinstance(entry, _Candidate) and (
                unit.scope != entry.scope or target.migration_key(unit.id) != entry.key
            ):
                raise ValidationError("memory identity differs from physical record")
            auth = security.auth
            if auth.is_expired() or registry.is_revoked(auth):
                raise AuthenticationError("authentication failed")
            allowed = _allowed(session, security, unit.scope, unit.id)
            event = AuditEvent(
                id=uuid.uuid4().hex,
                actor=auth.actor,
                target=unit.scope,
                target_id=unit.id,
                action="migrate_memory",
                layer="api",
                decision="allow" if allowed else "deny",
                occurred_at=datetime.now(UTC),
                detail={
                    "operation": operation,
                    "request_id": security.request_id,
                    "phase": "attempt",
                },
            )
            # Audit failure prevents mutation; exports are never written into audit detail.
            session.audit.record(event)
            if not allowed:
                raise PermissionDeniedError("migrate_memory")
            outcome = (
                "plaintext"
                if operation == "inspect"
                else target.migrate_plaintext(
                    unit.scope,
                    target.migration_key(unit.id),
                    raw,
                    operation=operation,
                    existing_only=isinstance(entry, _Candidate),
                )
            )
            committed = outcome in {"migrated", "cleared"}
            session.audit.record(
                replace(
                    event,
                    id=uuid.uuid4().hex,
                    occurred_at=datetime.now(UTC),
                    detail={**event.detail, "phase": "result", "status": outcome},
                )
            )
            results.append(MigrationItemResult(index, outcome, committed=committed))
        except Exception as exc:
            results.append(
                MigrationItemResult(
                    index,
                    "failed",
                    _code(exc),
                    committed=committed or isinstance(exc, MigrationVerificationError),
                )
            )
    status = "failed" if any(item.status == "failed" for item in results) else "complete"
    return MigrationResult(operation, status, tuple(results))


def _scan(session, security, *, operation, port, limit, cursor, batch_size, batch_bytes):
    inventory = session.storage.encryption_inventory()
    if not isinstance(inventory, dict) or set(inventory) != set(session.storage.capabilities()):
        raise UnsupportedCapabilityError("migration_inventory", "manager", "storage")
    for capability, ports in inventory.items():
        if (
            not isinstance(ports, dict)
            or not ports
            or any(
                getattr(session.storage, capability.port_method)(name) is not store
                for name, store in ports.items()
            )
        ):
            raise UnsupportedCapabilityError("migration_inventory", "ports", "storage")
    selected = [
        (capability, name, target)
        for capability in (StorageCapability.KV, StorageCapability.FS)
        for name, target in sorted(inventory.get(capability, {}).items())
    ]
    if port is not None:
        if port not in inventory.get(StorageCapability.KV, {}):
            raise ValidationError("unknown migration KV port")
        selected = [
            entry for entry in selected if entry[0] is StorageCapability.KV and entry[1] == port
        ]
    identities = {}
    prepared = []
    results = []
    # Resolve and deduplicate the complete selected inventory before any mutation.
    for capability, name, target in selected:
        source = f"{capability.namespace}.{name}"
        try:
            if not _is_target(target):
                raise UnsupportedCapabilityError("plaintext_migration", name, "storage")
            scan_supported = getattr(target, "migration_scan_supported", None)
            if not callable(scan_supported) or scan_supported() is not True:
                raise UnsupportedCapabilityError("bounded_migration_scan", name, "storage")
            if security.auth.role is not Role.ROOT:
                target.migration_validate_scope(security.auth.actor)
            identity = target.migration_identity()
            binding = target.security.encryption_binding
            protection = (binding.provider, binding.format) if binding else target
            if identity in identities:
                if identities[identity] != protection:
                    raise ValidationError("physical aliases use conflicting protection")
                continue
            identities[identity] = protection
            prepared.append((capability, name, target))
        except Exception as exc:
            results.append(MigrationItemResult(len(results) + 1, "failed", _code(exc), source))
    if results and operation != "inspect":
        return MigrationResult(operation, "failed", tuple(results))
    identity = hashlib.sha256(repr([
        (capability.namespace, name, target.migration_identity())
        for capability, name, target in prepared
    ]).encode()).hexdigest()
    resume = _resume_cursor(cursor, identity, operation)
    if resume and resume["source"] not in {
        f"{capability.namespace}.{name}" for capability, name, _ in prepared
    }:
        raise ValidationError("invalid migration cursor")
    checkpoint = cursor or ""
    total_bytes = count = 0
    reached_source = resume is None
    for capability, name, target in prepared:
        source = f"{capability.namespace}.{name}"
        if not reached_source:
            if source != resume["source"]:
                continue
            reached_source = True
        try:
            # Scope authorization precedes loading its values. Unauthorized scopes
            # produce no count, ID or result entry.
            for scope in sorted(target.migration_scopes(), key=_scope_key):
                if resume and source == resume["source"] and (
                    _scope_key(scope) < tuple(resume["scope"])
                ):
                    continue
                if _allowed(session, security, scope):
                    after = resume["after"] if resume and source == resume["source"] and (
                        _scope_key(scope) == tuple(resume["scope"])
                    ) else ""
                    for record in target.migration_records(
                        scope, after=after, max_value_bytes=2 * limit + 65536
                    ):
                        size = len(record.value) if record.value is not None else 0
                        if count >= batch_size or (count and total_bytes + size > batch_bytes):
                            return MigrationResult(
                                operation, "partial", tuple(results), cursor=checkpoint
                            )
                        if len(record.cursor) > 32768:
                            raise UnsupportedCapabilityError("bounded_cursor", name, "storage")
                        outcome = _process(
                            session, security, [_Candidate(scope, record.key, record.value)],
                            operation=operation, port=name, limit=limit, target=target,
                        )
                        results.extend(
                            MigrationItemResult(len(results) + 1, item.status, item.error,
                                                source, item.committed)
                            for item in outcome.items
                        )
                        count += 1
                        total_bytes += size
                        checkpoint = _next_cursor(identity, source, scope, record.cursor, operation)
        except Exception as exc:
            results.append(MigrationItemResult(len(results) + 1, "failed", _code(exc), source))
    status = "failed" if any(item.status == "failed" for item in results) else "complete"
    return MigrationResult(operation, status, tuple(results))


def migrate_plaintext_memories(
    records: Iterable[bytes] | None = None,
    *,
    config: Mapping[str, Any] | Config,
    credentials: Credentials,
    operation: str = "inspect",
    port: str | None = None,
    cursor: str | None = None,
    batch_size: int = 10000,
    batch_bytes: int = 268435456,
) -> MigrationResult:
    """Explicit configured-source scan/import/clear with configured authentication.

    config/port are trusted deployment selection, never untrusted remote payload.
    Each item is atomic; earlier successes remain committed on partial failure.
    Caller owns the export file. No startup/background migration is installed.
    """
    from .assembly import assemble_maintenance

    session = None
    result = MigrationResult(operation, "failed", error="operation_failed")
    try:
        if operation not in {"inspect", "migrate", "clear"}:
            raise ValidationError("invalid migration operation")
        for budget, maximum in ((batch_size, 10000), (batch_bytes, 268435456)):
            if type(budget) is not int or not 1 <= budget <= maximum:
                raise ValidationError("invalid maintenance batch budget")
        if records is not None and cursor:
            raise ValidationError("migration cursor requires storage scan")
        if port is not None:
            if not isinstance(port, str) or not port:
                raise ValidationError("invalid migration port")
        globals_config = (
            config.context().globals if isinstance(config, Config) else config.get("globals", {})
        )
        limit = globals_config.get("storage_encryption_max_plaintext_bytes", 67108864)
        if type(limit) is not int or not 0 < limit <= 67108864:
            raise ValidationError("invalid migration size limit")
        session = assemble_maintenance(config)
        security = session.authenticate(credentials)
        if not security.has_valid_origin():
            raise AuthenticationError("authentication failed")
        if records is None:
            result = _scan(session, security, operation=operation, port=port, limit=limit,
                           cursor=cursor, batch_size=batch_size, batch_bytes=batch_bytes)
        else:
            result = _process(
                session, security, records, operation=operation, port=port or "default", limit=limit
            )
    except Exception as exc:
        result = MigrationResult(operation, "failed", error=_code(exc))
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                result = MigrationResult(
                    operation, "failed", result.items, "cleanup_failed", result.cursor
                )
    return result
