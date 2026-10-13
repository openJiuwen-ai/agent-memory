# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PR1-1 装配期开关与部署保护声明；不把声明伪装成远端密码学验证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.config.config_source import ConfigSource
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig

_STORES = (
    "kv_store",
    "fs_store",
    "vector_store",
    "fulltext_store",
    "graph_store",
    "fusion_store",
    "entity_store",
    "markdown_store",
    "shadow_index",
)


def encryption_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValidationError("storage_encryption_enabled must be true or false")


@dataclass(frozen=True)
class EncryptionPolicy:
    enabled: bool
    cryptography: str

    @classmethod
    def from_context(cls, ctx: AssemblyContext) -> EncryptionPolicy:
        enabled = encryption_enabled(ctx.globals.get("storage_encryption_enabled", False))
        name = ctx.globals.get("storage_encryption_cryptography", "default")
        if not isinstance(name, str) or not name:
            raise ValidationError("storage_encryption_cryptography must name a shared provider")
        return cls(enabled, name)


@dataclass(frozen=True)
class AtRestLocation:
    """A bound location may be exempt from deployment evidence, never from location guards."""

    value: Any
    requires_declaration: bool = True


def _normalize_locations(label: str, locations: object) -> dict[str, AtRestLocation]:
    if not isinstance(locations, dict):
        raise ValidationError(f"{label}: invalid at_rest_locations contract")
    normalized = {}
    for field, value in locations.items():
        if not isinstance(field, str) or not field:
            raise ValidationError(f"{label}: invalid at_rest_locations contract")
        location = value if isinstance(value, AtRestLocation) else AtRestLocation(value)
        if not location.value or not isinstance(location.requires_declaration, bool):
            raise ValidationError(f"{label}: invalid at_rest_locations contract")
        normalized[field] = location
    return normalized


def _check_declaration(
    config: ComponentConfig, label: str, locations: dict[str, AtRestLocation]
) -> None:
    required = {field: loc.value for field, loc in locations.items() if loc.requires_declaration}
    if not required:
        return
    declaration = config.params.get("at_rest")
    if not isinstance(declaration, dict):
        raise ValidationError(f"{label} requires an at_rest deployment declaration")
    if declaration.get("method") not in {"volume", "backend"}:
        raise ValidationError(f"{label}.at_rest.method must be volume or backend")
    reference = declaration.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError(f"{label}.at_rest.reference must identify deployment evidence")
    bindings = declaration.get("bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise ValidationError(f"{label}.at_rest.bindings must bind protected storage locations")
    if set(bindings) != set(required):
        raise ValidationError(f"{label}.at_rest.bindings must contain {tuple(required)}")
    for field, actual in required.items():
        if bindings[field] != actual:
            raise ValidationError(f"{label}: protected {field} does not match configuration")


def deployment_bindings(ctx: AssemblyContext) -> dict[str, str | None]:
    """核查部署声明并固定全部可切换位置，包括免部署声明的内存位置。

    at_rest 声明来自可信服务端配置，表示部署责任方已核查；不是本进程检查远端磁盘
    的证明。仓库 Docker launcher 在实际验证 dm-crypt 挂载后生成对应声明。
    """
    policy = EncryptionPolicy.from_context(ctx)
    if not policy.enabled:
        return {}
    guarded: dict[str, str | None] = {}
    for namespace in (*_STORES, "audit", "permission", "grant_store", "delegation_store"):
        if namespace in {"kv_store", "fs_store"}:
            if any(spec.new_instance for spec in ctx.namespaces.get(namespace, {}).values()):
                raise ValidationError(
                    "encrypted KV/FS ports require shared named instances (new_instance=false)"
                )
            # 两类字节端口由 StoreManager 装配加密，不依赖远端声明。
            continue
        for name, spec in ctx.namespaces.get(namespace, {}).items():
            config = ComponentConfig(spec.params, ctx, spec.target, name)
            declared_locations = Factory.for_namespace(namespace).inspect_config(
                spec.target, "at_rest_locations", config
            )
            label = f"{namespace}.{name}"
            locations = _normalize_locations(label, declared_locations)
            _check_declaration(config, label, locations)
            for field, location in locations.items():
                actual = str(location.value)
                # Connection implementations currently fetch namespace.field; retain named keys
                # as well so a future named binding cannot silently bypass the deployment check.
                guarded[f"{namespace}.{name}.{field}"] = actual
                shared_key = f"{namespace}.{field}"
                if shared_key in guarded and guarded[shared_key] != actual:
                    raise ValidationError(
                        "encrypted instances sharing a late-binding key need one storage location"
                    )
                guarded[shared_key] = actual
    return guarded


class EncryptionBoundConfigSource(ConfigSource):
    """部署加密启用后拒绝将受保护索引连接热切换到未经重新核查的位置。"""

    def __init__(self, inner: ConfigSource, bindings: dict[str, str | None]) -> None:
        self._inner = inner
        self._snapshots = {key: inner.fetch(key) for key in bindings}
        if any(
            value is not None and value != bindings[key] for key, value in self._snapshots.items()
        ):
            raise ValidationError("ConfigSource location differs from the protected deployment")

    def fetch(self, key: str) -> str | None:
        value = self._inner.fetch(key)
        if key in self._snapshots and value != self._snapshots[key]:
            raise ValidationError(
                "encrypted deployment location changed; reassemble after verification"
            )
        return value

    def health(self) -> None:
        self._inner.health()
