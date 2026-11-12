# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stable model handles: atomically swap complete clients, retain in-flight leases."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.security.types import reveal_secret
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.keys import resolve_instance_namespace
from jiuwen_memory.config.project import project_assembly_values


def frozen_source(ctx: AssemblyContext) -> ConfigSource:
    values = project_assembly_values(ctx)
    for name, spec in ctx.namespaces.get("llm", {}).items():
        namespace = resolve_instance_namespace("llm", name, spec.params)
        for parameter, short in (
            ("llm_model", "model"),
            ("llm_base_url", "base_url"),
            ("llm_api_key", "api_key"),
        ):
            value = spec.params.get(parameter, ctx.globals.get(parameter))
            if value is not None:
                values[f"{namespace}.{short}"] = reveal_secret(value)
    return DictConfigSource(values)


@dataclass(eq=False)
class _Lease:
    model: LLM
    active: int = 0
    retired: bool = False


def _close(model):
    close = getattr(model, "close", None)
    if callable(close):
        close()


class ManagedLLM(LLM):
    def __init__(self, source, model):
        self.source = source
        self.current = _Lease(model)

    @contextmanager
    def lease(self):
        with self.source.lock:
            if self.source.closed:
                raise ValidationError("managed model is closed")
            lease = self.current
            lease.active += 1
        try:
            yield lease.model
        finally:
            with self.source.lock:
                lease.active -= 1
                dispose = lease.retired and not lease.active
            if dispose:
                self.source.dispose(lease.model)

    def plugin_type(self):
        return PluginType.LLM

    def health(self):
        with self.lease() as model:
            model.health()

    def chat(self, messages, **options):
        with self.lease() as model:
            return model.chat(messages, **options)


class ModelRuntimeSource(ConfigSource):
    """Only managed LLMs use frozen endpoints; original Prompt readers are untouched."""

    def __init__(self, fallback, context, version):
        self.fallback = fallback
        self.context = context
        self.version = version
        self.lock = RLock()
        self.models: dict[str, list[ManagedLLM]] = {}
        self.closed = False
        self.cleanup_errors = []

    def fetch(self, key):
        # Preserve original non-model readers. Expose current frozen endpoints for SDK reads.
        for name, spec in self.context.namespaces.get("llm", {}).items():
            namespace = resolve_instance_namespace("llm", name, spec.params)
            if key in {f"{namespace}.{field}" for field in ("model", "base_url", "api_key")}:
                return frozen_source(self.context).fetch(key)
        return self.fallback.fetch(key)

    def for_model(self):
        return frozen_source(self.context)

    def build_model(self, name):
        """Construct late/nonshared handles from the entire current applied snapshot.

        The caller may still hold its original AssemblyContext. Do not mix that
        context's constructor defaults with the current endpoint projection.
        """
        with self.lock:
            if self.closed:
                raise ValidationError("managed model is closed")
            model = self._build_current(self.context, name)
            handle = ManagedLLM(self, model)
            self.models.setdefault(name, []).append(handle)
            return handle

    @staticmethod
    def _build_current(context, name):
        fresh = AssemblyContext(globals=dict(context.globals), namespaces=dict(context.namespaces))
        ConfigSourceProducer.put("default", frozen_source(fresh), ctx=fresh)
        spec = fresh.lookup("llm", name)
        model = LlmProducer.build(spec.target, dict(spec.params), fresh, name=name)
        if not isinstance(model, LLM):
            raise ValidationError("model builder returned an invalid model")
        return model

    def apply(self, context, version):
        with self.lock:
            if self.closed or version < self.version:
                return False
            if version == self.version:
                return True
            prepared = []
            try:
                for name, handles in self.models.items():
                    for handle in handles:
                        model = self._build_current(context, name)
                        prepared.append((handle, model))
            except Exception:
                for _, model in prepared:
                    self.dispose(model)
                raise
            retired = []
            for handle, model in prepared:
                old = handle.current
                old.retired = True
                handle.current = _Lease(model)
                if not old.active:
                    retired.append(old.model)
            self.context = context
            self.version = version
        for model in retired:
            self.dispose(model)
        return True

    def dispose(self, model):
        try:
            _close(model)
        except Exception as error:
            # Applying a new client already succeeded. Report cleanup at Runtime shutdown,
            # rather than falsely claiming the active configuration was not applied.
            with self.lock:
                self.cleanup_errors.append(
                    RuntimeError("model client cleanup failed: " + type(error).__name__)
                )

    def close(self):
        retired = []
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for handles in self.models.values():
                for handle in handles:
                    handle.current.retired = True
                    if not handle.current.active:
                        retired.append(handle.current.model)
        for model in retired:
            self.dispose(model)
        if self.cleanup_errors:
            raise ExceptionGroup("managed model client cleanup failed", self.cleanup_errors)
