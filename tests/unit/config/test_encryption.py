"""PR1-1 switch, complete backend declarations and protected connection binding."""

from __future__ import annotations

import copy
import io

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel, _register_all
from jiuwen_memory.common.errors import BackendError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.config.config_source import ConfigSourceProducer
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.context import AssemblyContext, ComponentConfig
from jiuwen_memory.config.encryption import (
    AtRestLocation,
    EncryptionBoundConfigSource,
    EncryptionPolicy,
    deployment_bindings,
)
from jiuwen_memory.storage.encryption import protect_ports
from jiuwen_memory.storage.fs import FsProducer
from jiuwen_memory.storage.fs_impl.in_memory_fs_store import InMemoryFSStore
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.shadow import ShadowIndexProducer

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", space="product", user="alice")


@pytest.fixture(autouse=True)
def registered_plugins():
    _register_all()
    Factory.reset_all()
    yield
    Factory.reset_all()


def _config(enabled=True):
    return {
        "globals": {"storage_encryption_enabled": enabled},
        "cryptography": {"default": {"target": "local", "params": {"key_provider": "default"}}},
        "key_provider": {"default": {"target": "local", "params": {"key_hex": "11" * 32}}},
    }


def _declared(namespace, target, bindings, name="default"):
    data = _config()
    data[namespace] = {
        name: {
            "target": target,
            "params": {
                **bindings,
                "at_rest": {
                    "method": "volume",
                    "reference": "operator:volume-01",
                    "bindings": dict(bindings),
                },
            },
        }
    }
    return data


@pytest.mark.parametrize("enabled", [False, "false", True, "true"])
def test_switch_handles_yaml_and_environment_booleans(enabled):
    policy = EncryptionPolicy.from_context(AssemblyContext.from_dict(_config(enabled)))
    assert policy.enabled == (enabled is True or enabled == "true")


@pytest.mark.parametrize("enabled", [None, 0, 1, "flase", "yes", [], {}])
def test_switch_rejects_invalid_values_instead_of_silent_plaintext(enabled):
    with pytest.raises(ValidationError):
        deployment_bindings(AssemblyContext.from_dict(_config(enabled)))


@pytest.mark.parametrize(
    "namespace,target,bindings",
    [
        ("fulltext_store", "elasticsearch", {"hosts": "https://es.internal:9200"}),
        ("entity_store", "elasticsearch", {"hosts": ["https://es.internal:9200"]}),
        ("vector_store", "milvus", {"uri": "https://milvus.internal:19530"}),
        ("vector_store", "pgvector", {"dsn": "postgresql://db/internal"}),
        ("graph_store", "nano_graphrag", {"working_dir": "/encrypted/graph"}),
        (
            "fusion_store",
            "milvus_graph",
            {"uri": "https://milvus.internal:19530", "working_dir": "/encrypted/fusion"},
        ),
        ("audit", "sqlite", {"db_path": "/encrypted/audit.db"}),
        ("permission", "sqlite", {"db_path": "/encrypted/permission.db"}),
        ("permission", "space_aware", {"db_path": "/encrypted/permission.db"}),
    ],
)
def test_deployment_contract_covers_all_persistent_index_and_governance_backends(
    namespace,
    target,
    bindings,
):
    data = _declared(namespace, target, bindings)
    guarded = deployment_bindings(AssemblyContext.from_dict(data))
    assert set(guarded) == {
        f"{namespace}.{prefix}{field}" for prefix in ("", "default.") for field in bindings
    }
    del data[namespace]["default"]["params"]["at_rest"]
    with pytest.raises(ValidationError):
        deployment_bindings(AssemblyContext.from_dict(data))


@pytest.mark.parametrize("broken", ["method", "reference", "bindings", "location"])
def test_incomplete_or_mismatched_protection_is_rejected(broken):
    data = _declared("fulltext_store", "elasticsearch", {"hosts": "https://es:9200"})
    declaration = data["fulltext_store"]["default"]["params"]["at_rest"]
    if broken == "location":
        declaration["bindings"]["hosts"] = "https://other:9200"
    else:
        declaration[broken] = {"method": "none", "reference": "", "bindings": {}}[broken]
    with pytest.raises(ValidationError):
        deployment_bindings(AssemblyContext.from_dict(data))


def test_all_named_layer_ports_require_protection():
    data = _declared("fulltext_store", "elasticsearch", {"hosts": "https://es:9200"})
    data["fulltext_store"]["layers_l1"] = {
        "target": "elasticsearch",
        "params": {"hosts": "https://es:9200"},
    }
    with pytest.raises(ValidationError, match="layers_l1"):
        deployment_bindings(AssemblyContext.from_dict(data))


def test_disabled_switch_preserves_existing_config_without_declarations():
    data = _declared("fulltext_store", "elasticsearch", {"hosts": "https://es:9200"})
    data["globals"]["storage_encryption_enabled"] = False
    del data["fulltext_store"]["default"]["params"]["at_rest"]
    assert deployment_bindings(AssemblyContext.from_dict(data)) == {}


def test_memory_only_and_disabled_entity_need_no_volume_declaration():
    data = _config()
    data.update(
        {
            "vector_store": {"default": "memory"},
            "entity_store": {"default": {"target": "elasticsearch", "params": {}}},
            "audit": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        }
    )
    assert deployment_bindings(AssemblyContext.from_dict(data)) == {
        "audit.db_path": ":memory:",
        "audit.default.db_path": ":memory:",
    }


def test_connection_source_rejects_initial_and_later_unverified_locations():
    bindings = {"vector_store.uri": "https://milvus:19530"}
    with pytest.raises(ValidationError):
        EncryptionBoundConfigSource(
            DictConfigSource({"vector_store.uri": "https://other"}), bindings
        )
    inner = DictConfigSource(bindings)
    guarded = EncryptionBoundConfigSource(inner, bindings)
    assert guarded.fetch("vector_store.uri") == bindings["vector_store.uri"]
    inner.put("vector_store.uri", "https://other")
    with pytest.raises(ValidationError):
        guarded.fetch("vector_store.uri")
    inner.put("llm.model", "new-model")
    assert guarded.fetch("llm.model") == "new-model"


def test_automatic_wrapping_protects_injected_and_all_named_ports():
    Factory.reset_all()
    data = _config()
    ctx = AssemblyContext.from_dict(data)
    raw_kv, raw_fs = InMemoryKVStore(), InMemoryFSStore()
    raw_second = InMemoryKVStore()
    kv, fs = protect_ports(
        ComponentConfig({}, ctx), {"default": raw_kv, "messages": raw_second}, {"default": raw_fs}
    )
    for name, raw in (("default", raw_kv), ("messages", raw_second)):
        kv[name].insert(SCOPE, "key", b"secret")
        assert raw.get(SCOPE, "key").startswith(b"ENC1")
        assert kv[name].get(SCOPE, "key") == b"secret"
        assert kv[name] is KvProducer.build_named(name, ctx)
    ref = fs["default"].insert(SCOPE, "sample.bin", io.BytesIO(b"secret"))
    with raw_fs.get(SCOPE, ref) as stream:
        assert stream.read().startswith(b"ENC1")
    assert fs["default"] is FsProducer.build_named("default", ctx)
    assert (
        fs["default"].security.encryption_binding.provider
        is kv["default"].security.encryption_binding.provider
    )


def test_explicit_wrapper_and_public_raw_port_share_one_ciphertext_layer():
    Factory.reset_all()
    ctx = AssemblyContext.from_dict(_config())
    from jiuwen_memory.common.security.cryptography import CryptographyProducer

    provider = CryptographyProducer.build_named("default", ctx)
    raw = InMemoryKVStore()
    explicit = EncryptedKVStore(raw, provider)
    ports, _ = protect_ports(ComponentConfig({}, ctx), {"default": explicit, "raw": raw}, {})
    assert ports["default"] is explicit
    ports["raw"].insert(SCOPE, "key", b"secret")
    assert explicit.get(SCOPE, "key") == b"secret"
    assert ports["raw"].get(SCOPE, "key") == b"secret"


def test_kernel_switch_wraps_injected_kv_and_keeps_defaults_off():
    raw = InMemoryKVStore()
    kernel = _build_kernel(kv=raw, config=_config())
    try:
        kernel.kv.insert(SCOPE, "key", b"secret")
        assert raw.get(SCOPE, "key").startswith(b"ENC1")
        assert kernel.kv.get(SCOPE, "key") == b"secret"
    finally:
        kernel.ingest_jobs.close()
        kernel.scheduler.shutdown()
    raw_off = InMemoryKVStore()
    kernel_off = _build_kernel(kv=raw_off)
    try:
        kernel_off.kv.insert(SCOPE, "key", b"secret")
        assert raw_off.get(SCOPE, "key") == b"secret"
    finally:
        kernel_off.ingest_jobs.close()
        kernel_off.scheduler.shutdown()


def test_missing_key_rejects_assembly_without_accepting_writes():
    data = _config()
    data["key_provider"]["default"]["params"] = {"key_file": "", "key_env": ""}
    with pytest.raises(BackendError, match="requires"):
        _build_kernel(config=data)


def test_multiple_instances_cannot_reuse_a_guarded_key_for_different_locations():
    data = _declared("fulltext_store", "elasticsearch", {"hosts": "https://es:9200"})
    other = copy.deepcopy(data["fulltext_store"]["default"])
    other["params"]["hosts"] = "https://other:9200"
    other["params"]["at_rest"]["bindings"]["hosts"] = "https://other:9200"
    data["fulltext_store"]["other"] = other
    with pytest.raises(ValidationError):
        deployment_bindings(AssemblyContext.from_dict(data))


def test_entity_endpoint_alias_cannot_bypass_the_deployment_check():
    data = _config()
    data["entity_store"] = {
        "default": {
            "target": "elasticsearch",
            "params": {
                "endpoint": "https://es:9200",
            },
        }
    }
    with pytest.raises(ValidationError):
        deployment_bindings(AssemblyContext.from_dict(data))
    params = data["entity_store"]["default"]["params"]
    params["at_rest"] = {
        "method": "backend",
        "reference": "ops:checked",
        "bindings": {"endpoint": params["endpoint"]},
    }
    assert "entity_store.endpoint" in deployment_bindings(AssemblyContext.from_dict(data))


def test_public_raw_port_preserves_explicit_nondefault_key_provider():
    from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
        LocalEnvelopeCryptographyProvider,
        LocalKeyProvider,
    )

    Factory.reset_all()
    ctx = AssemblyContext.from_dict(_config())
    configured = LocalEnvelopeCryptographyProvider(
        LocalKeyProvider(key_file="", key_env="", key_hex="33" * 32)
    )
    raw = InMemoryKVStore()
    explicit = EncryptedKVStore(raw, configured)
    ports, _ = protect_ports(ComponentConfig({}, ctx), {"default": explicit, "raw": raw}, {})
    ports["raw"].insert(SCOPE, "key", b"secret")
    assert explicit.get(SCOPE, "key") == b"secret"
    assert ports["raw"].security.encryption_binding.provider is configured


@pytest.mark.parametrize("namespace", ["kv_store", "fs_store"])
def test_nonshared_byte_ports_cannot_bypass_cache_protection(namespace):
    data = _config()
    data[namespace] = {"default": {"target": "memory", "new_instance": True}}
    with pytest.raises(ValidationError, match="new_instance"):
        deployment_bindings(AssemblyContext.from_dict(data))


def test_conflicting_explicit_providers_on_one_raw_store_are_rejected():
    from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
        LocalEnvelopeCryptographyProvider,
        LocalKeyProvider,
    )

    ctx = AssemblyContext.from_dict(_config())
    raw = InMemoryKVStore()
    a = LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="11" * 32))
    b = LocalEnvelopeCryptographyProvider(LocalKeyProvider(key_hex="22" * 32))
    with pytest.raises(ValidationError, match="different"):
        protect_ports(
            ComponentConfig({}, ctx),
            {"a": EncryptedKVStore(raw, a), "b": EncryptedKVStore(raw, b)},
            {},
        )


@pytest.mark.parametrize(
    "namespace,target,field",
    [("markdown_store", "local", "root"), ("shadow_index", "sqlite", "db_path")],
)
def test_document_storage_requires_explicit_protected_location(namespace, target, field):
    data = _config()
    data[namespace] = {"default": {"target": target}}
    with pytest.raises(ValidationError, match="invalid at_rest_locations"):
        deployment_bindings(AssemblyContext.from_dict(data))
    data = _declared(namespace, target, {field: "/encrypted/documents"})
    guarded = deployment_bindings(AssemblyContext.from_dict(data))
    key = f"{namespace}.{field}"
    source = DictConfigSource({})
    bound = EncryptionBoundConfigSource(source, guarded)
    assert bound.fetch(key) is None
    source.put(key, "/plaintext/documents")
    with pytest.raises(ValidationError, match="changed"):
        bound.fetch(key)


@pytest.mark.parametrize(
    "namespace,target,params",
    [
        ("shadow_index", "sqlite", {"db_path": ":memory:"}),
        ("audit", "sqlite", {"db_path": ":memory:"}),
        ("audit", "sqlite", {}),
        ("permission", "sqlite", {"db_path": ":memory:"}),
        ("permission", "sqlite", {}),
        ("permission", "space_aware", {"db_path": ":memory:"}),
        ("permission", "space_aware", {}),
    ],
)
def test_memory_sqlite_needs_no_declaration_but_keeps_its_location_guard(namespace, target, params):
    data = _config()
    data[namespace] = {"default": {"target": target, "params": params}}
    bindings = deployment_bindings(AssemblyContext.from_dict(data))
    assert bindings == {f"{namespace}.{prefix}db_path": ":memory:" for prefix in ("", "default.")}
    for key in bindings:
        with pytest.raises(ValidationError, match="differs"):
            EncryptionBoundConfigSource(DictConfigSource({key: "/unprotected.db"}), bindings)
        source = DictConfigSource(bindings)
        bound = EncryptionBoundConfigSource(source, bindings)
        assert bound.fetch(key) == ":memory:"
        source.put(key, "/unprotected.db")
        with pytest.raises(ValidationError, match="changed"):
            bound.fetch(key)


def test_document_ports_pass_public_manager_policy_with_verified_paths(tmp_path):
    data = _declared("markdown_store", "local", {"root": str(tmp_path / "documents")})
    shadow = _declared("shadow_index", "sqlite", {"db_path": str(tmp_path / "shadow.db")})
    data["shadow_index"] = shadow["shadow_index"]
    data["globals"]["watch_document"] = False
    runtime = _build_kernel(config=data)
    try:
        inventory = runtime.storage.encryption_inventory()
        from jiuwen_memory.storage.store_manager import StorageCapability

        assert inventory[StorageCapability.MARKDOWN]["default"] is runtime.storage.markdown()
        assert (
            inventory[StorageCapability.DOCUMENT_SHADOW]["default"]
            is runtime.storage.shadow_index()
        )
    finally:
        runtime.close()
        runtime.ingest_jobs.close()
        runtime.scheduler.shutdown()


@pytest.mark.parametrize("key", ["shadow_index.db_path", "shadow_index.default.db_path"])
def test_public_runtime_rejects_initial_override_of_memory_shadow_location(tmp_path, key):
    destination = tmp_path / "unprotected.db"
    data = _config()
    data["globals"]["watch_document"] = False
    data["shadow_index"] = {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}
    data["config_source"] = {
        "default": {"target": "dict", "params": {"values": {key: str(destination)}}}
    }
    runtime = None
    try:
        with pytest.raises(ValidationError, match="differs"):
            runtime = assemble_runtime(config=data)
    finally:
        if runtime is not None:
            runtime.close()
    assert not destination.exists()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("opened", [True, False])
def test_public_runtime_blocks_memory_shadow_disk_switch_only_when_enabled(
    tmp_path, enabled, opened
):
    source = DictConfigSource({"shadow_index.db_path": ":memory:"})
    destination = tmp_path / "unprotected.db"
    data = _config(enabled)
    data["globals"]["watch_document"] = False
    data["shadow_index"] = {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}}
    data["config_source"] = {"default": {"target": "mutable_memory_shadow"}}
    with ConfigSourceProducer.registration_scope():
        ConfigSourceProducer.register("mutable_memory_shadow")(lambda config: source)
        runtime = assemble_runtime(config=data)
        try:
            shadow = ShadowIndexProducer.build_named("default", AssemblyContext.from_dict(data))
            existing = MemoryUnit(id="existing", scope=SCOPE, segments=[Segment(content="private")])
            if opened:
                shadow.insert_units(SCOPE, [existing])
                assert shadow.get_units(SCOPE, [existing.id]) == [existing]
            source.put("shadow_index.db_path", str(destination))
            incoming = MemoryUnit(
                id="incoming", scope=SCOPE, segments=[Segment(content="new private")]
            )
            if enabled:
                with pytest.raises(ValidationError, match="changed"):
                    shadow.insert_units(SCOPE, [incoming])
                assert not destination.exists()
                source.put("shadow_index.db_path", ":memory:")
                assert shadow.get_units(SCOPE, [existing.id]) == ([existing] if opened else [])
            else:
                shadow.insert_units(SCOPE, [incoming])
                assert destination.is_file()
                assert shadow.get_units(SCOPE, [incoming.id]) == [incoming]
        finally:
            runtime.close()


@pytest.mark.parametrize("value,requires_declaration", [("", False), (":memory:", "false")])
def test_invalid_location_capability_is_rejected(value, requires_declaration):
    from jiuwen_memory.storage.vector import VectorProducer

    with VectorProducer.registration_scope():
        VectorProducer.register(
            "invalid_locations",
            config_inspectors={
                "at_rest_locations": lambda config: {
                    "cache": AtRestLocation(value, requires_declaration=requires_declaration)
                }
            },
        )(lambda config: None)
        data = _config()
        data["vector_store"] = {"default": {"target": "invalid_locations"}}
        with pytest.raises(ValidationError, match="invalid at_rest_locations"):
            deployment_bindings(AssemblyContext.from_dict(data))


def test_foreign_plugin_can_exempt_a_location_without_exempting_its_guard():
    from jiuwen_memory.storage.vector import VectorProducer

    with VectorProducer.registration_scope():
        VectorProducer.register(
            "mixed_locations",
            config_inspectors={
                "at_rest_locations": lambda config: {
                    "bucket": config.get("bucket"),
                    "cache": AtRestLocation("volatile", requires_declaration=False),
                }
            },
        )(lambda config: None)
        data = _declared("vector_store", "mixed_locations", {"bucket": "encrypted-bucket"})
        bindings = deployment_bindings(AssemblyContext.from_dict(data))
        assert bindings["vector_store.cache"] == "volatile"
        assert bindings["vector_store.bucket"] == "encrypted-bucket"
        with pytest.raises(ValidationError, match="differs"):
            EncryptionBoundConfigSource(DictConfigSource({"vector_store.cache": "disk"}), bindings)


def test_memory_and_file_instances_cannot_share_one_late_binding_key():
    data = _declared("shadow_index", "sqlite", {"db_path": "/encrypted/shadow.db"}, name="file")
    data["shadow_index"]["default"] = {"target": "sqlite", "params": {"db_path": ":memory:"}}
    with pytest.raises(ValidationError, match="one storage location"):
        deployment_bindings(AssemblyContext.from_dict(data))
