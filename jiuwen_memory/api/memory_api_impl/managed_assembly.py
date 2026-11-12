# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Small same-runtime model configuration assembly seam; no Prompt management."""

from __future__ import annotations

from dataclasses import replace

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.config.managed_fields import ManagedFields
from jiuwen_memory.config.managed_service import ManagedConfigService
from jiuwen_memory.config.model_runtime import ModelRuntimeSource


class ManagedAssemblyPlan:
    def __init__(self, store, llm_instances, authorize, url_allowed, locked_keys):
        if not callable(authorize):
            raise ValidationError("managed configuration requires a trusted authorizer")
        if url_allowed is not None and not callable(url_allowed):
            raise ValidationError("model URL policy must be callable")
        if isinstance(llm_instances, str | bytes) or any(
            not isinstance(name, str) or not name for name in llm_instances
        ):
            raise ValidationError("managed LLM instances must be named")
        self.store, self.names = store, tuple(llm_instances)
        self.authorize, self.url_allowed, self.locked_keys = authorize, url_allowed, locked_keys
        self.service = self.source = self.configuration = None
        self.snapshot = self.prepared = None

    def close(self):
        if self.source is not None:
            self.source.close()

    def prepare(self, base):
        if as_bool(base.globals.get("storage_encryption_enabled"), default=False):
            raise ValidationError(
                "managed config overlay is not supported in encrypted storage mode"
            )
        # Preserve explicit ConfigSource endpoint overrides as startup input.
        from jiuwen_memory.config.context import AssemblyContext
        from jiuwen_memory.config.keys import resolve_instance_namespace

        source_spec = base.namespaces.get("config_source", {}).get("default")
        overrides = source_spec.params.get("values", {}) if source_spec else {}
        namespaces = {ns: dict(items) for ns, items in base.namespaces.items()}
        for name, spec in base.namespaces.get("llm", {}).items():
            namespace = resolve_instance_namespace("llm", name, spec.params)
            params = dict(spec.params)
            for short, parameter in (
                ("model", "llm_model"),
                ("api_key", "llm_api_key"),
                ("base_url", "llm_base_url"),
            ):
                if f"{namespace}.{short}" in overrides:
                    params[parameter] = overrides[f"{namespace}.{short}"]
            namespaces["llm"][name] = replace(spec, params=params)
        base = AssemblyContext(globals=dict(base.globals), namespaces=namespaces)
        fields = ManagedFields(base, self.names, url_allowed=self.url_allowed)
        self.service = ManagedConfigService(base, fields, self.store, locked_keys=self.locked_keys)
        self.snapshot = self.store.read()
        self.prepared = self.service.assembly_context(self.snapshot, warn=True)
        return self.prepared

    def validate_version(self):
        if self.store.read().version != self.snapshot.version:
            raise ConflictError(message="configuration changed during runtime assembly")

    def bind_source(self, fallback):
        from jiuwen_memory.api.managed_configuration import ManagedConfiguration

        self.validate_version()
        self.source = ModelRuntimeSource(fallback, self.prepared, self.snapshot.version)
        self.service.bind_runtime(self.source)
        self.configuration = ManagedConfiguration(self.service, self.authorize)
        return self.source

    def finalize(self, ctx):
        from jiuwen_memory.common.llm.base import LlmProducer

        names = set(ctx.instance_cache.instances.get(LlmProducer, {})) | set(self.source.models)
        self.service.set_runtime_usage(frozenset(names))
