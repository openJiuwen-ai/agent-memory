# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Maintenance-only composition root; does not assemble MemoryAPI or background jobs."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.audit.base import AuditLogger, AuditProducer
from jiuwen_memory.common.audit.protected_audit_logger import ProtectedAuditLogger
from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import AuthenticationError, RateLimitedError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import setup_logging
from jiuwen_memory.common.security._assembly_lifecycle import _assembly_lifecycle
from jiuwen_memory.common.security.authentication.credential_registry import (
    CredentialStatusRegistry,
)
from jiuwen_memory.common.security.capabilities import (
    SecurityLifecycleCapability,
    SecurityResourceOwner,
    credential_sources,
)
from jiuwen_memory.common.security.request_context import new_request_context
from jiuwen_memory.common.security.runtime import SecurityRuntime, SecurityRuntimeProducer
from jiuwen_memory.common.security.types import Credentials, RequestSecurityContext, Surface
from jiuwen_memory.config import Config
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.config_source_impl import register_config_sources
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.config.defaults import ROOT_PARAMS, default_context
from jiuwen_memory.config.encryption import EncryptionBoundConfigSource, deployment_bindings
from jiuwen_memory.control.bootstrap import register_controllers
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.store_manager import StoreManager, StoreManagerProducer


@dataclass(frozen=True)
class MaintenanceSession:
    storage: StoreManager
    security_runtime: SecurityRuntime
    audit: AuditLogger
    resources: SecurityResourceOwner

    def authenticate(self, credentials: Credentials) -> RequestSecurityContext:
        runtime = self.security_runtime
        if self.resources.is_closed():
            raise AuthenticationError("authentication failed")
        if not runtime.workload_guard.acquire():
            raise RateLimitedError("maintenance authentication workload budget exhausted")
        try:
            auth = runtime.authenticator.authenticate(credentials)
            registry = CredentialStatusRegistry()
            for kind, issuer, store in credential_sources(runtime.authenticator):
                registry.register(kind, issuer, store)
            if auth.is_expired() or registry.is_revoked(auth):
                raise AuthenticationError("authentication failed")
            return new_request_context(auth, surface=Surface.CLI)
        finally:
            runtime.workload_guard.release()

    def close(self) -> None:
        self.resources.close()


def assemble_maintenance(config: Mapping[str, Any] | Config) -> MaintenanceSession:
    register_plugins()
    register_backends()
    # Register configured policy/space plugins without constructing an Engine or jobs.
    register_controllers()
    register_config_sources()
    parsed = config if isinstance(config, Config) else Config.from_dict(config)
    ctx = default_context().merged(parsed.context(known_top_names=Factory.known_top_names()))
    names = sorted(ctx.namespaces.get(SecurityRuntimeProducer.TOP_NAME, {}))
    if not names:
        raise AuthenticationError("authentication failed")
    if "default" in names:
        selected = "default"
    elif len(names) == 1:
        selected = names[0]
    else:
        raise ValidationError("maintenance security runtime selection is ambiguous")
    root = ComponentConfig(dict(ROOT_PARAMS), ctx, "maintenance", "plaintext_maintenance")
    with _assembly_lifecycle(ctx) as lifecycle:
        setup_logging(root)
        bindings = deployment_bindings(ctx)
        source = ConfigSourceProducer.dep(root, default="yaml_defaults")
        if not isinstance(source, ConfigSource):
            raise ValidationError("maintenance requires a ConfigSource")
        if bindings:
            source = EncryptionBoundConfigSource(source, bindings)
        ConfigSourceProducer.put("default", source, ctx=ctx)
        storage = StoreManagerProducer.resolve(root)
        audit = AuditProducer.dep(root, default="sqlite")
        runtime = SecurityRuntimeProducer.build_named(selected, ctx)
        if not isinstance(runtime, SecurityRuntime):
            raise ValidationError("maintenance requires a SecurityRuntime")
        if runtime.authorizer.is_test_only():
            raise ValidationError("maintenance refuses test-only authorizers")
        runtime.health()
        provider = runtime.audit_integrity_provider
        if provider is not None:
            audit = ProtectedAuditLogger(provider, audit)
        elif ctx.namespaces.get("audit_integrity"):
            raise ValidationError("configured audit integrity is not bound to security runtime")
        # Every factory-created resource is owned; dependencies use their public contract.
        roots = tuple(
            resource for resource in lifecycle.created
            if not isinstance(resource, SecurityLifecycleCapability)
        )
        resources = SecurityResourceOwner(roots)
        for resource in lifecycle.created:
            if isinstance(resource, SecurityLifecycleCapability):
                resources.share_with(resource.security_resource_owner())
        lifecycle.bind(resources)
        return MaintenanceSession(storage, runtime, audit, resources)
