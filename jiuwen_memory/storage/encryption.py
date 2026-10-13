# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""经公开保护契约与 Producer 装配加密；公共 manager 解析边界负责验证策略。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.cryptography import CryptographyProducer
from jiuwen_memory.config.context import ComponentConfig
from jiuwen_memory.config.encryption import EncryptionPolicy, deployment_bindings
from jiuwen_memory.storage.fs import FsProducer
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.security import StoreEncryptionBinding

if TYPE_CHECKING:
    from jiuwen_memory.storage.store_manager import StoreManager


def _binding(store: Any) -> StoreEncryptionBinding | None:
    binding = store.security.encryption_binding
    if binding is None:
        return None
    if not isinstance(binding, StoreEncryptionBinding):
        raise ValidationError("invalid store encryption dependency contract")
    if not binding.format or binding.inner is store or not store.security.enabled():
        raise ValidationError("invalid store encryption dependency contract")
    return binding


def protect_ports(
    config: ComponentConfig, kv: dict[str, Any], fs: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = EncryptionPolicy.from_context(config.ctx)
    if not policy.enabled:
        return kv, fs
    deployment_bindings(config.ctx)
    existing: dict[int, Any] = {}
    for ports in (kv, fs):
        for store in ports.values():
            binding = _binding(store)
            if binding is None:
                continue
            previous = existing.get(id(binding.inner))
            if previous is not None:
                other = _binding(previous)
                if other.provider is not binding.provider or other.format != binding.format:
                    raise ValidationError("one raw store cannot use different encryption bindings")
            existing[id(binding.inner)] = store

    for producer, ports, kind, dependency in (
        (KvProducer, kv, "kv", "raw_kv_store"),
        (FsProducer, fs, "fs", "inner"),
    ):
        for name, store in ports.items():
            protected = store if store.security.enabled() else existing.get(id(store))
            if protected is None:
                spec = config.get(f"storage_encryption_{kind}", {"target": "encrypted"})
                if not isinstance(spec, dict) or not isinstance(spec.get("target"), str):
                    raise ValidationError(f"storage_encryption_{kind} must select a wrapper target")
                params = spec.get("params", {})
                if not isinstance(params, dict) or {dependency, "cryptography"} & params.keys():
                    raise ValidationError("automatic encryption dependencies are policy-controlled")
                # 显式注入具名依赖；合成名只用于缓存引用，不暴露为 manager 端口。
                raw_name = f"__storage_encryption_raw_{id(store)}__"
                producer.put(raw_name, store)
                params = {**params, dependency: raw_name, "cryptography": policy.cryptography}
                if kind == "fs":
                    params.setdefault(
                        "max_plaintext_bytes",
                        config.get("storage_encryption_max_plaintext_bytes", 67108864),
                    )
                    params.setdefault(
                        "max_ciphertext_bytes",
                        config.get("storage_encryption_max_ciphertext_bytes", 0),
                    )
                protected = producer.build(spec["target"], params, config.ctx)
                binding = _binding(protected)
                provider = CryptographyProducer.build_named(policy.cryptography, config.ctx)
                if (
                    binding is None
                    or binding.inner is not store
                    or binding.provider is not provider
                ):
                    raise ValidationError(
                        "encryption wrapper did not bind the configured dependencies"
                    )
                existing[id(store)] = protected
            protected.security.health()  # Missing keys must fail before accepting writes.
            ports[name] = protected
            producer.put(name, protected)
    return kv, fs


def validate_manager_encryption(config: ComponentConfig, manager: StoreManager) -> None:
    """对所有 manager 插件执行相同的静态加密准入检查。"""
    if not EncryptionPolicy.from_context(config.ctx).enabled:
        return
    from jiuwen_memory.storage.store_manager import StorageCapability

    deployment_bindings(config.ctx)
    inventory = manager.encryption_inventory()
    if not isinstance(inventory, dict) or set(inventory) != set(manager.capabilities()):
        raise ValidationError("StoreManager encryption inventory must cover every capability")
    for capability, ports in inventory.items():
        if (
            not isinstance(capability, StorageCapability)
            or not isinstance(ports, dict)
            or not ports
        ):
            raise ValidationError("invalid StoreManager encryption inventory")
        namespace = capability.namespace
        configured = config.ctx.namespaces.get(namespace, {})
        for name, store in ports.items():
            if getattr(manager, capability.port_method)(name) is not store:
                raise ValidationError("StoreManager encryption inventory differs from public ports")
            if capability in {StorageCapability.KV, StorageCapability.FS}:
                if not store.security.enabled():
                    raise ValidationError(f"{namespace}.{name}: encryption is required")
                store.security.health()
            elif name not in configured and not store.security.enabled():
                raise ValidationError(f"{namespace}.{name}: protection contract is unknown")
        if capability in {StorageCapability.KV, StorageCapability.FS} and not set(
            configured
        ) <= set(ports):
            raise ValidationError(f"{namespace}: encryption inventory omits configured ports")
    for capability in (StorageCapability.KV, StorageCapability.FS):
        if config.ctx.namespaces.get(f"{capability.value}_store") and capability not in inventory:
            raise ValidationError(f"{capability.value}_store: encryption inventory is missing")
