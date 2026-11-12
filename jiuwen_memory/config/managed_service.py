# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Versioned model settings: persist first, then atomically apply complete clients."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jiuwen_memory.common._support import (
    read_outbound_call_policy,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.types import SecretValue, reveal_secret
from jiuwen_memory.config.binding import resolve_connection_url
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.config.managed_fields import apply_assembly_overrides
from jiuwen_memory.config.overlay_store import OverlaySaveResult

_LOG = get_logger(__name__)
_ENDPOINT_FIELDS = ("llm_base_url", "llm_api_key", "llm_model")
_ENDPOINT_CHANGE_ERROR = "更换模型服务时，请一起填写服务网址、API Key 和模型名。"
_CREDENTIAL_PREFIX = "memory-portal-endpoint-v1:"


def _model_url(context, name):
    spec = context.lookup("llm", name)
    if spec.target not in {"openai", "dashscope"}:
        return None
    url = spec.params.get("llm_base_url", context.globals.get("llm_base_url"))
    if isinstance(url, str) and url.strip():
        return url.strip()
    if spec.target == "dashscope":
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")


def _url_origin(url):
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValidationError("model URL is invalid") from error
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


def _model_origin(context, name):
    return _url_origin(_model_url(context, name))


def _credential_for_origin(stored, origin):
    if not stored.startswith(_CREDENTIAL_PREFIX):
        # Old credentials have no origin. Echo is offline and cannot send the key.
        return stored if origin is None else None
    try:
        bound_origin, value = json.loads(stored.removeprefix(_CREDENTIAL_PREFIX))
    except (TypeError, ValueError):
        return None
    if origin is not None and bound_origin != list(origin):
        return None
    return value if isinstance(value, str) and value else None


class ManagedConfigService:
    def __init__(self, base, fields, store, runtime_source=None, *, locked_keys=frozenset()):
        if not locked_keys <= fields.keys:
            raise ValidationError("locked fields must belong to managed fields")
        self._base, self._fields, self._store = base, fields, store
        self._source = runtime_source
        self._locked_keys = locked_keys
        self._lock = RLock()
        self.credentials = None
        self._credential_reason = "宿主未提供可靠凭据持久化能力"
        create = getattr(store, "credential_store", None)
        if callable(create):
            try:
                self.credentials = create()
            except Exception:
                # Existing saved credentials must never silently fall back to startup values.
                if any(k.endswith(".llm_api_key") for k in store.read().values):
                    raise
                self._credential_reason = "受保护凭据存储不可用"
        self._active_llms = None

    def bind_runtime(self, source):
        self._source = source

    def set_runtime_usage(self, active_llms):
        self._active_llms = frozenset(active_llms)

    def _reason(self, key):
        field = self._fields.fields[key]
        if key in self._locked_keys:
            return "由宿主锁定"
        if self._active_llms is not None and field.instance not in (
            self._source.models if self._source is not None else self._active_llms
        ):
            return "当前实例未装配此模型"
        if field.parameter == "llm_api_key" and self.credentials is None:
            return self._credential_reason
        return self._fields.readonly_reason(key)

    def _active_values(self, values):
        return {k: v for k, v in values.items() if k in self._fields.keys and not self._reason(k)}

    def assembly_context(self, snapshot=None, *, warn=False):
        snapshot = snapshot or self._store.read()
        context, ignored = self._resolve_overlay(snapshot.values)
        if warn and ignored:
            _LOG.warning("Portal model endpoint settings skipped: fields=%s", sorted(ignored))
        return context

    def _resolve_overlay(self, saved, *, strict_instances=frozenset()):
        values = self._active_values(saved)
        context = apply_assembly_overrides(self._base, values, self._fields.fields)
        materialized = dict(values)
        ignored = set()
        for name in self._model_instances():
            prefix = f"llm.{name}.params."
            url_key, api_key = prefix + "llm_base_url", prefix + "llm_api_key"
            url = _model_url(context, name)
            origin = _url_origin(url)
            key_value = None
            incomplete = (
                origin is not None and origin != _model_origin(self._base, name)
                and (url_key not in values or api_key not in values)
            )
            if api_key in values and not incomplete:
                key_value = _credential_for_origin(self.credentials.get(values[api_key]), origin)
            if incomplete or (api_key in values and key_value is None):
                if name in strict_instances:
                    raise ValidationError(_ENDPOINT_CHANGE_ERROR)
                group = {f"llm.{name}.target", *(prefix + field for field in _ENDPOINT_FIELDS)}
                if self._base.lookup("llm", name).target != "dashscope":
                    group.add(prefix + "enable_thinking")
                ignored.update(group & saved.keys())
                for key in group:
                    values.pop(key, None)
                    materialized.pop(key, None)
            elif key_value is not None:
                materialized[api_key] = SecretValue(key_value)
                if url is not None:
                    # Use the same URL even if the SDK's environment default later changes.
                    materialized[url_key] = url
        self._fields.validate(values)
        return apply_assembly_overrides(self._base, materialized, self._fields.fields), ignored

    def _model_instances(self):
        return {field.instance for field in self._fields.fields.values()}

    def _validate_endpoint_changes(self, context, snapshot, changes, restore):
        before, _ = self._resolve_overlay(snapshot.values)
        for name in self._model_instances():
            origin = _model_origin(context, name)
            if origin is None or origin == _model_origin(before, name):
                continue
            keys = {f"llm.{name}.params.{field}" for field in _ENDPOINT_FIELDS}
            if all(isinstance(changes.get(key), str) and changes[key].strip() for key in keys):
                continue
            # The startup URL and its original credential may be restored as one group.
            if (
                origin == _model_origin(self._base, name)
                and not keys & ((set(snapshot.values) | set(changes)) - set(restore))
            ):
                continue
            raise ValidationError(_ENDPOINT_CHANGE_ERROR)

    def read(self):
        with self._lock:
            snapshot = self._store.read()
            running = self._source.context if self._source is not None else None
            applied = self._source.version if self._source is not None else None
            saved_context, ignored_endpoint_fields = self._resolve_overlay(snapshot.values)
            descriptions = []
            defaults = {
                "llm_temperature": 0.0,
                "llm_max_tokens": 4096,
                "llm_timeout": 300,
                "llm_max_retries": 0,
                "enable_thinking": False,
            }
            parameters = (
                "target",
                "llm_model",
                "llm_base_url",
                "llm_api_key",
                "llm_temperature",
                "llm_max_tokens",
                "llm_timeout",
                "llm_max_retries",
                "enable_thinking",
            )
            order = {name: index for index, name in enumerate(parameters)}

            def value(ctx, field):
                if ctx is None:
                    return None
                spec = ctx.lookup("llm", field.instance)
                if field.parameter == "target":
                    return spec.target
                return spec.params.get(
                    field.parameter,
                    ctx.globals.get(field.parameter, defaults.get(field.parameter)),
                )

            for key, field in sorted(
                self._fields.fields.items(),
                key=lambda item: (item[1].instance, order[item[1].parameter]),
            ):
                if self._active_llms is not None and field.instance not in (
                    self._source.models if self._source is not None else self._active_llms
                ):
                    continue

                base, effective = value(self._base, field), value(running, field)
                saved = snapshot.values.get(key)
                secret = field.parameter == "llm_api_key"
                configured = bool(reveal_secret(value(saved_context, field))) if secret else None
                if secret:
                    base = effective = saved = None
                elif field.parameter == "llm_base_url":
                    base, effective, saved = map(_safe_url, (base, effective, saved))
                reason = self._reason(key)
                descriptions.append(
                    {
                        "key": key,
                        "base": base,
                        "saved": saved,
                        "has_override": key in snapshot.values,
                        "endpoint_ignored": key in ignored_endpoint_fields,
                        "effective": effective,
                        "effective_known": running is not None
                        and not secret
                        and effective is not None,
                        "requires_reassembly": False,
                        "apply_mode": "next_call",
                        "writable": not reason,
                        "reason": reason,
                        "secret": secret,
                        "visible_for_targets": ["dashscope"]
                        if field.parameter == "enable_thinking"
                        else [],
                        "configured": configured,
                        "options": list(self._fields.supported_targets(field.instance))
                        if field.parameter == "target"
                        else [],
                    }
                )
            for name in self._active_llms or ():
                if f"llm.{name}.target" not in self._fields.fields:
                    spec = self._base.lookup("llm", name)
                    descriptions.append(
                        {
                            "key": f"llm.{name}.target",
                            "base": spec.target,
                            "saved": None,
                            "effective": spec.target,
                            "has_override": False,
                            "effective_known": True,
                            "writable": False,
                            "options": [spec.target],
                            "requires_reassembly": False,
                            "reason": "自定义模型暂不支持配置修改",
                        }
                    )
            return {
                "version": snapshot.version,
                "applied_version": applied,
                "apply_state": "applied" if applied == snapshot.version else "apply_failed",
                "ignored_overrides": len(
                    (set(snapshot.values) - self._fields.keys) | ignored_endpoint_fields
                ),
                "ignored_endpoint_fields": sorted(ignored_endpoint_fields),
                "fields": descriptions,
                "instance": self._instance_info(),
            }

    def _instance_info(self):
        from jiuwen_memory.common._support import as_bool

        embedder = self._base.namespaces.get("embedder", {}).get("default")
        document = self._base.namespaces.get("markdown_store", {}).get("default")
        document_root = None
        if document is not None:
            document_root = resolve_connection_url(
                self._source, namespace="markdown_store", field="root",
                fallback=document.params.get("root"),
            )
        model = None
        dimension = self._base.globals.get("embedder_dim")
        if embedder is not None:
            dimension = embedder.params.get("embedder_dim", dimension)
            model = (
                embedder.target
                if embedder.target == "hashing"
                else embedder.params.get(
                    "embedder_model", self._base.globals.get("embedder_model", embedder.target)
                )
            )
        return {
            "storage_mode": "document"
            if as_bool(self._base.globals.get("write_document"), default=False)
            else "kv",
            "embedding_model": model,
            "embedding_dimension": dimension,
            "document_root": document_root,
        }

    def _validate_context(self, context):
        for name in self._model_instances():
            spec = context.lookup("llm", name)
            if spec.target not in {"openai", "dashscope"}:
                continue
            component = ComponentConfig(
                params=dict(spec.params), ctx=context, target=spec.target, name=name
            )
            read_outbound_call_policy(component, "llm")
            ssl = read_outbound_ssl(component, "llm")
            if ssl.verify:
                require_https(component.get("llm_base_url", ""), component="LLM", param="llm")
                require_ca_file(ssl.ca_cert, component="LLM", param="llm")

    def _validate_temperature_changes(self, context, values, changed_keys):
        for key in changed_keys:
            field = self._fields.fields[key]
            if field.parameter not in {"target", "llm_temperature"}:
                continue
            value = values.get(f"llm.{field.instance}.params.llm_temperature")
            if value is None:
                continue
            target = context.lookup("llm", field.instance).target
            exceeds_openai_limit = target == "openai" and value > 2
            exceeds_dashscope_limit = target == "dashscope" and value >= 2
            if value < 0 or exceeds_openai_limit or exceeds_dashscope_limit:
                raise ValidationError("model numeric value is invalid")

    def save(self, expected_version, changes: Mapping[str, Any], *, restore: Sequence[str] = ()):
        with self._lock:
            if type(expected_version) is not int or expected_version < 0:
                raise ValidationError("configuration version must be a non-negative integer")
            if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
                raise ValidationError("configuration changes must be a string-keyed object")
            if (
                not isinstance(restore, Sequence)
                or isinstance(restore, str | bytes)
                or any(not isinstance(k, str) for k in restore)
            ):
                raise ValidationError("restore must contain field names")
            keys = set(changes) | set(restore)
            if keys - self._fields.keys or any(self._reason(k) for k in keys):
                raise ValidationError("configuration contains locked or unsupported fields")
            if set(changes) & set(restore):
                raise ValidationError("cannot change and restore the same field")
            snapshot = self._store.read()
            if snapshot.version != expected_version:
                raise ConflictError(message="configuration overlay version changed")
            values = dict(snapshot.values)
            prepared = []
            try:
                new_keys = {}
                for key, val in changes.items():
                    if key.endswith(".llm_api_key"):
                        if not isinstance(val, str):
                            raise ValidationError("model credential must be a string")
                        if not val:
                            continue
                        if not val.strip():
                            raise ValidationError("model credential must not be whitespace")
                        new_keys[key] = val
                    else:
                        values[key] = val
                for key in restore:
                    values.pop(key, None)
                # Resolve changed and restored providers against the final overlay.
                for instance in self._model_instances():
                    target_key = f"llm.{instance}.target"
                    if target_key not in keys:
                        continue
                    target = values.get(target_key, self._base.lookup("llm", instance).target)
                    if target != "dashscope":
                        values.pop(f"llm.{instance}.params.enable_thinking", None)
                endpoints = apply_assembly_overrides(
                    self._base, self._active_values(values), self._fields.fields
                )
                for key, val in new_keys.items():
                    origin = _model_origin(endpoints, self._fields.fields[key].instance)
                    reference = self.credentials.put(_CREDENTIAL_PREFIX + json.dumps([origin, val]))
                    prepared.append(reference)
                    values[key] = reference
                endpoint_params = {"target", *_ENDPOINT_FIELDS}
                strict_instances = {
                    self._fields.fields[key].instance for key in keys
                    if self._fields.fields[key].parameter in endpoint_params
                }
                context, _ = self._resolve_overlay(values, strict_instances=strict_instances)
                self._validate_endpoint_changes(context, snapshot, changes, restore)
                self._validate_temperature_changes(context, self._active_values(values), keys)
                self._validate_context(context)
                committed = self._store.commit(expected_version, values)
            except Exception:
                for reference in prepared:
                    try:
                        self.credentials.discard(reference)
                    except Exception:
                        _LOG.warning("model credential cleanup deferred")
                raise
            result = self._apply(committed)
            if self.credentials is not None:
                retired = [
                    value
                    for key, value in snapshot.values.items()
                    if key.endswith(".llm_api_key") and values.get(key) != value
                ]
                try:
                    self.credentials.retire(retired)
                except Exception:
                    _LOG.warning("retired model credential cleanup deferred")
            return result

    def _apply(self, snapshot):
        try:
            if self._source is None:
                return OverlaySaveResult(snapshot, False)
            context = self.assembly_context(snapshot, warn=True)
            applied = self._source.apply(context, snapshot.version)
        except Exception as error:
            # Do not expose outbound credentials via arbitrary builder exceptions.
            _LOG.warning(
                "model configuration apply failed: version=%d error_type=%s",
                snapshot.version, type(error).__name__,
            )
            applied = False
        return OverlaySaveResult(snapshot, applied)

    def retry_publication(self):
        """Apply the latest saved model version."""
        with self._lock:
            return self._apply(self._store.read())


def _safe_url(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, "", "", ""))
    except ValueError:
        return None
