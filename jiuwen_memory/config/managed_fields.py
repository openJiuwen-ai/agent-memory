# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""定义可管理的模型字段，并按字段映射合成装配配置。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.keys import resolve_instance_namespace

_MODELS = frozenset({"echo", "openai", "dashscope"})
_PARAMS = (
    "llm_model",
    "llm_base_url",
    "llm_api_key",
    "llm_temperature",
    "llm_max_tokens",
    "llm_timeout",
    "llm_max_retries",
    "enable_thinking",
)


@dataclass(frozen=True)
class AssemblyField:
    """由可信装配方定义的键与具体实例字段映射；不从字符串路径猜测实例名。"""

    namespace: str
    instance: str
    parameter: str


def apply_assembly_overrides(
    base: AssemblyContext,
    values: Mapping[str, Any],
    fields: Mapping[str, AssemblyField],
) -> AssemblyContext:
    """只合成明确映射的字段，保留其他参数和依赖引用。"""
    unknown = set(values) - set(fields)
    if unknown:
        raise ValidationError("configuration overlay contains unsupported fields")
    namespaces = {namespace: dict(instances) for namespace, instances in base.namespaces.items()}
    globals_ = dict(base.globals)
    for key, value in values.items():
        field = fields[key]
        try:
            original = namespaces[field.namespace][field.instance]
        except KeyError as exc:
            raise ValidationError("configuration overlay refers to an unknown instance") from exc
        if field.parameter == "target":
            if not isinstance(value, str) or not value:
                raise ValidationError("component target must be a non-empty string")
            replacement = replace(original, target=value, params=dict(original.params))
        else:
            params = dict(original.params)
            params[field.parameter] = value
            replacement = replace(original, params=params)
        namespaces[field.namespace][field.instance] = replacement
    return AssemblyContext(globals=globals_, namespaces=namespaces)


class ManagedFields:
    """可信装配方显式提供实际引用，未知插件和凭据不会成为可写字段。"""

    def __init__(
        self,
        context: AssemblyContext,
        llm_instances: Sequence[str],
        *,
        url_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        register_plugins()
        self._context = context
        self.fields: dict[str, AssemblyField] = {}
        self._targets: dict[str, str] = {}
        self._url_allowed = url_allowed
        namespaces: dict[str, list[str]] = {}
        for name, spec in context.namespaces.get("llm", {}).items():
            namespace = resolve_instance_namespace("llm", name, spec.params)
            namespaces.setdefault(namespace, []).append(name)
        shared_names = set()
        for names in namespaces.values():
            if len(names) > 1:
                shared_names.update(names)
        self._shared_names = frozenset(shared_names)
        for instance in dict.fromkeys(llm_instances):
            spec = context.lookup("llm", instance)
            if not self._supports_target(spec.target, instance):
                continue
            self._targets[instance] = spec.target
            self.fields[f"llm.{instance}.target"] = AssemblyField("llm", instance, "target")
            for parameter in _PARAMS:
                self.fields[f"llm.{instance}.params.{parameter}"] = AssemblyField(
                    "llm", instance, parameter
                )

    def _supports_target(self, target: str, instance: str) -> bool:
        if target not in _MODELS:
            return False
        spec = self._context.lookup("llm", instance)
        component = ComponentConfig(
            params=dict(spec.params), ctx=self._context, target=target, name=instance
        )
        try:
            return LlmProducer.inspect_config(target, "portal_model_fields", component) is True
        except ValidationError:
            return False

    def supported_targets(self, instance: str) -> tuple[str, ...]:
        return tuple(
            target
            for target in ("echo", "openai", "dashscope")
            if self._supports_target(target, instance)
        )

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.fields)

    def readonly_reason(self, key: str) -> str:
        field = self.fields.get(key)
        if (
            field
            and field.instance in self._shared_names
            and field.parameter in {"llm_model", "llm_base_url", "llm_api_key"}
        ):
            return "此配置命名空间由多个实例共享，不能单独修改"
        return ""

    def validate(self, values: Mapping[str, Any]) -> None:
        """校验整个候选覆盖，调用方应在事务前调用并另外执行身份与宿主策略检查。"""
        if set(values) - self.keys:
            raise ValidationError("configuration contains an unsupported field")
        if any(self.readonly_reason(key) for key in values):
            raise ValidationError("configuration contains a shared LLM namespace field")
        targets = dict(self._targets)
        for key, value in values.items():
            field = self.fields.get(key)
            if field is not None and field.parameter == "target":
                if not isinstance(value, str) or not self._supports_target(value, field.instance):
                    raise ValidationError("unsupported LLM target")
                targets[field.instance] = value
        for key, value in values.items():
            field = self.fields[key]
            parameter = field.parameter
            if parameter == "target":
                continue
            if parameter == "enable_thinking":
                if targets[field.instance] != "dashscope" or not isinstance(value, bool):
                    raise ValidationError("enable_thinking requires DashScope and a boolean")
            elif parameter == "llm_api_key":
                if not isinstance(value, str) or not value:
                    raise ValidationError("model credential reference is invalid")
            elif parameter == "llm_model":
                if not isinstance(value, str) or not value.strip():
                    raise ValidationError("model name must be non-empty")
            elif parameter == "llm_base_url":
                if not isinstance(value, str):
                    raise ValidationError("model URL must be a string")
                try:
                    parsed = urlsplit(value)
                    hostname = parsed.hostname
                    parsed.port  # 非法端口应视为校验失败
                except ValueError as exc:
                    raise ValidationError("model URL is invalid") from exc
                has_extra_parts = any((
                    parsed.username is not None, parsed.password is not None,
                    parsed.query, parsed.fragment,
                ))
                valid_url = (
                    parsed.scheme in {"http", "https"} and bool(hostname) and not has_extra_parts
                )
                if not valid_url or (
                    self._url_allowed is not None and not self._url_allowed(value)
                ):
                    raise ValidationError("model URL is not permitted by the host")
            elif parameter in {"llm_temperature", "llm_timeout"}:
                is_number = isinstance(value, int | float) and not isinstance(value, bool)
                is_finite = is_number and math.isfinite(value)
                if not is_finite or (parameter == "llm_timeout" and value <= 0):
                    raise ValidationError("model numeric value is invalid")
            elif parameter in {"llm_max_tokens", "llm_max_retries"}:
                minimum = 1 if parameter == "llm_max_tokens" else 0
                if type(value) is not int or value < minimum:
                    raise ValidationError("model integer value is invalid")
