# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""替换插件后仍由公共装配契约执行静态加密策略。"""

import io
from contextlib import ExitStack

import pytest

from jiuwen_memory.api import assemble, assemble_runtime
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel, _register_all
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.security.cryptography import CryptographyProducer
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.encryption import deployment_bindings
from jiuwen_memory.storage.encryption import protect_ports
from jiuwen_memory.storage.fs import FsProducer, FSStore
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import EnabledStoreSecurity, StoreEncryptionBinding
from jiuwen_memory.storage.store_manager import (
    StorageCapability,
    StoreManager,
    StoreManagerProducer,
)
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import CompositeStoreManager

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")


@pytest.fixture(autouse=True)
def registered_plugins():
    _register_all()
    namespaces = (
        "store_manager",
        "kv_store",
        "fs_store",
        "audit",
        "permission",
        "vector_store",
        "entity_store",
        "markdown_store",
        "shadow_index",
    )
    with ExitStack() as stack:
        for namespace in namespaces:
            stack.enter_context(Factory.for_namespace(namespace).registration_scope())
        Factory.reset_all()
        try:
            yield
        finally:
            Factory.reset_all()


def config(enabled=True):
    return {
        "globals": {"storage_encryption_enabled": enabled},
        "cryptography": {"default": {"target": "local", "params": {"key_provider": "default"}}},
        "key_provider": {"default": {"target": "local", "params": {"key_hex": "11" * 32}}},
    }


class ForeignManager(StoreManager):
    """独立 manager 实现；默认保留旧插件未声明保护拓扑的行为。"""

    def __init__(self, delegate):
        self.delegate = delegate

    @property
    def security(self):
        return self.delegate.security

    def capabilities(self):
        return self.delegate.capabilities()

    def markdown(self, name="default"):
        return self.delegate.markdown(name)

    def has_markdown(self, name="default"):
        return self.delegate.has_markdown(name)

    def shadow_index(self, name="default"):
        return self.delegate.shadow_index(name)

    def has_shadow_index(self, name="default"):
        return self.delegate.has_shadow_index(name)

    def domain_store(self, name="default"):
        return self.delegate.domain_store(name)

    def has_domain_store(self, name="default"):
        return self.delegate.has_domain_store(name)

    def kv(self, name="default"):
        return self.delegate.kv(name)

    def fs(self, name="default"):
        return self.delegate.fs(name)

    def vector(self, name="default"):
        return self.delegate.vector(name)

    def fulltext(self, name="default"):
        return self.delegate.fulltext(name)

    def graph(self, name="default"):
        return self.delegate.graph(name)

    def fusion(self, name="default"):
        return self.delegate.fusion(name)

    def entity(self, name="default"):
        return self.delegate.entity(name)

    def health(self):
        self.delegate.health()


class DeclaredManager(ForeignManager):
    def encryption_inventory(self):
        return self.delegate.encryption_inventory()


@pytest.mark.parametrize("entry", [assemble, assemble_runtime])
@pytest.mark.parametrize("declared", [False, True])
def test_public_assembly_rejects_unprotected_replacement_manager(entry, declared):
    raw = InMemoryKVStore()
    replacement = DeclaredManager if declared else ForeignManager
    StoreManagerProducer.register("foreign")(lambda c: replacement(CompositeStoreManager(kv=raw)))
    data = config()
    data["key_provider"]["default"]["params"] = {"key_env": "", "key_file": ""}
    data["store_manager"] = {"default": {"target": "foreign"}}
    with pytest.raises(ValidationError, match="encryption"):
        entry(config=data)
    assert raw.scan(SCOPE) == []


def test_declared_replacement_manager_can_serve_encrypted_ports():
    StoreManagerProducer.register("foreign")(
        lambda c: DeclaredManager(CompositeStoreManager.from_config(c))
    )
    data = config()
    data["store_manager"] = {"default": {"target": "foreign"}}
    raw = InMemoryKVStore()
    kernel = _build_kernel(kv=raw, config=data)
    try:
        kernel.kv.insert(SCOPE, "key", b"plaintext")
        assert raw.get(SCOPE, "key").startswith(b"ENC1")
        assert kernel.kv.get(SCOPE, "key") == b"plaintext"
    finally:
        kernel.ingest_jobs.close()
        kernel.scheduler.shutdown()


def test_old_replacement_manager_remains_compatible_when_switch_is_off():
    StoreManagerProducer.register("foreign")(
        lambda c: ForeignManager(CompositeStoreManager.from_config(c))
    )
    data = config(False)
    data["store_manager"] = {"default": {"target": "foreign"}}
    runtime = assemble_runtime(config=data)
    runtime.close()


@pytest.mark.parametrize("bad", ["missing", "missing_name", "raw", "other_object"])
def test_partial_or_false_manager_inventory_is_rejected(bad):
    class BrokenManager(DeclaredManager):
        raw = InMemoryKVStore()

        def kv(self, name="default"):
            return self.raw if name == "hidden" else super().kv(name)

        def encryption_inventory(self):
            inventory = super().encryption_inventory()
            if bad == "missing":
                return {}
            if bad == "raw":
                inventory[StorageCapability.KV]["hidden"] = self.raw
            elif bad == "missing_name":
                del inventory[StorageCapability.KV]["extra"]
            else:
                inventory[StorageCapability.KV]["default"] = InMemoryKVStore()
            return inventory

    StoreManagerProducer.register("broken")(
        lambda c: BrokenManager(CompositeStoreManager.from_config(c))
    )
    data = config()
    data["store_manager"] = {"default": {"target": "broken"}}
    data["kv_store"] = {"extra": {"target": "memory"}}
    with pytest.raises(ValidationError):
        assemble(config=data)


class ForeignEncryptedKV(InMemoryKVStore):
    """不继承内置装饰器，只使用公开保护/依赖契约。"""

    def __init__(self, raw, provider):
        super().__init__()
        self.backend = raw
        self.provider = provider
        self.protection = EnabledStoreSecurity(
            provider.health, binding=StoreEncryptionBinding(raw, provider, "foreign/v1")
        )

    @property
    def security(self):
        return self.protection

    def insert(self, scope, key, value):
        context = CryptoContext(scope=scope, purpose="foreign", object_id=key)
        self.backend.insert(scope, key, self.provider.encrypt(value, context=context))

    def get(self, scope, key):
        context = CryptoContext(scope=scope, purpose="foreign", object_id=key)
        return self.provider.decrypt(self.backend.get(scope, key), context=context)


def test_configured_foreign_wrapper_and_raw_alias_reuse_one_protection_layer():
    built = []

    @KvProducer.register("foreign_encrypted")
    def build(c):
        wrapper = ForeignEncryptedKV(KvProducer.dep(c, "raw_kv_store"), CryptographyProducer.dep(c))
        built.append(wrapper)
        return wrapper

    data = config()
    data["globals"]["storage_encryption_kv"] = {"target": "foreign_encrypted"}
    ctx = AssemblyContext.from_dict(data)
    raw = InMemoryKVStore()
    ports, _ = protect_ports(ComponentConfig({}, ctx), {"default": raw, "alias": raw}, {})
    assert len(built) == 1
    assert ports["default"] is ports["alias"] is built[0]
    ports["alias"].insert(SCOPE, "key", b"plaintext")
    assert raw.get(SCOPE, "key").startswith(b"ENC1")
    assert ports["default"].get(SCOPE, "key") == b"plaintext"
    # 显式第三方装饰器与 raw 同时暴露时，不构建第二层，也不读取插件私有字段。
    ports, _ = protect_ports(ComponentConfig({}, ctx), {"explicit": built[0], "raw": raw}, {})
    assert ports["raw"] is ports["explicit"] is built[0]
    assert len(built) == 1


def test_foreign_fs_wrapper_can_be_selected_and_reused_without_core_type_checks():
    from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore

    class ForeignFS(FSStore):
        def __init__(self, delegate):
            self.delegate = delegate

        @property
        def security(self):
            return self.delegate.security

        def store_type(self):
            return self.delegate.store_type()

        def health(self):
            self.delegate.health()

        def object_id(self, scope, key):
            return self.delegate.object_id(scope, key)

        def insert(self, scope, key, stream):
            return self.delegate.insert(scope, key, stream)

        def update(self, scope, ref, stream):
            return self.delegate.update(scope, ref, stream)

        def get(self, scope, ref):
            return self.delegate.get(scope, ref)

        def stat(self, scope, ref):
            return self.delegate.stat(scope, ref)

        def delete(self, scope, ref):
            self.delegate.delete(scope, ref)

    FsProducer.register("foreign_fs")(
        lambda c: ForeignFS(FsProducer.build("encrypted", c.params, c.ctx))
    )
    data = config()
    data["globals"]["storage_encryption_fs"] = {"target": "foreign_fs"}
    ctx = AssemblyContext.from_dict(data)
    raw = InMemoryFSStore()
    _, ports = protect_ports(ComponentConfig({}, ctx), {}, {"default": raw, "alias": raw})
    assert isinstance(ports["default"], ForeignFS)
    assert ports["default"] is ports["alias"]
    ref = ports["default"].insert(SCOPE, "asset", io.BytesIO(b"private asset"))
    with raw.get(SCOPE, ref) as ciphertext:
        assert ciphertext.read().startswith(b"ENC1")
    with ports["alias"].get(SCOPE, ref) as plaintext:
        assert plaintext.read() == b"private asset"


@pytest.mark.parametrize("producer,key", [(KvProducer, "kv"), (FsProducer, "fs")])
def test_selected_wrapper_must_fulfill_dependency_and_protection_contract(producer, key):
    producer.register("unprotected")(
        lambda c: producer.dep(c, "raw_kv_store" if key == "kv" else "inner")
    )
    data = config()
    data["globals"][f"storage_encryption_{key}"] = {"target": "unprotected"}
    ctx = AssemblyContext.from_dict(data)
    with pytest.raises(ValidationError, match="dependencies"):
        if key == "kv":
            protect_ports(ComponentConfig({}, ctx), {"default": InMemoryKVStore()}, {})
        else:
            from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore

            protect_ports(ComponentConfig({}, ctx), {}, {"default": InMemoryFSStore()})


@pytest.mark.parametrize(
    "namespace",
    ["audit", "permission", "entity_store", "vector_store", "markdown_store", "shadow_index"],
)
@pytest.mark.parametrize("target", ["foreign", "memory"])
def test_unknown_persistence_capability_is_never_inferred_from_name_or_empty_config(
    namespace, target
):
    producer = Factory.for_namespace(namespace)
    producer.register(target)(lambda c: None)
    data = config()
    data[namespace] = {"default": {"target": target}}
    with pytest.raises(ValidationError, match="unknown"):
        deployment_bindings(AssemblyContext.from_dict(data))


@pytest.mark.parametrize(
    "namespace",
    ["audit", "permission", "entity_store", "vector_store", "markdown_store", "shadow_index"],
)
def test_foreign_persistence_plugin_declares_own_locations_without_core_changes(namespace):
    producer = Factory.for_namespace(namespace)
    producer.register(
        "foreign", config_inspectors={"at_rest_locations": lambda c: {"bucket": c.get("bucket")}}
    )(lambda c: None)
    data = config()
    params = {
        "bucket": "private-memory",
        "at_rest": {
            "method": "backend",
            "reference": "kms:operator-verified",
            "bindings": {"bucket": "private-memory"},
        },
    }
    data[namespace] = {"default": {"target": "foreign", "params": params}}
    assert (
        deployment_bindings(AssemblyContext.from_dict(data))[f"{namespace}.bucket"]
        == "private-memory"
    )
    params["at_rest"]["bindings"]["bucket"] = "other"
    with pytest.raises(ValidationError, match="does not match"):
        deployment_bindings(AssemblyContext.from_dict(data))
