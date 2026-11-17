# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""F09：物理资源标识与启动期自动拓扑登记。"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.storage._support import sanitize_resource_id
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.shard_routed_kv import ShardRoutedKVStore
from jiuwen_memory.storage.shard_routing import RuleBasedShardResolver
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import (
    _register_shard_topology,
)

pytestmark = pytest.mark.unit


class _DescribedKV(InMemoryKVStore):
    def __init__(self, descriptor: str) -> None:
        super().__init__()
        self._descriptor = descriptor

    def resource_descriptor(self) -> str:
        return self._descriptor


def _resolver() -> RuleBasedShardResolver:
    return RuleBasedShardResolver(
        shards=["working", "profile"], default_shard="working", rules=[]
    )


def _routed(
    *, placement: InMemoryKVStore, descriptors: dict[str, str]
) -> ShardRoutedKVStore:
    return ShardRoutedKVStore(
        resolver=_resolver(),
        passthrough=InMemoryKVStore(),
        placement=placement,
        shard_stores={
            shard: _DescribedKV(value) for shard, value in descriptors.items()
        },
    )


def test_sanitize_resource_id_strips_credentials() -> None:
    assert (
        sanitize_resource_id("postgresql://user:secret@db:5432/memory")
        == "db:5432/memory"
    )
    assert sanitize_resource_id("redis://:secret@cache:6379/0") == "cache:6379/0"


def test_sanitize_resource_id_redacts_non_url_credentials() -> None:
    """物理标识会持久化进拓扑登记：非 URL 形式也必须脱敏。"""
    libpq = sanitize_resource_id("host=db user=u password=s3cret dbname=memory")
    assert "s3cret" not in libpq
    assert libpq == "host=db user=u password=*** dbname=memory"
    assert (
        sanitize_resource_id("postgresql://user:s3cret@db/memory").count("s3cret") == 0
    )


def test_sqlite_descriptor_is_path_or_none(tmp_path) -> None:
    from jiuwen_memory.storage.kv_impl.sqlite_kv_store import SQLiteKVStore

    assert SQLiteKVStore(db_path=":memory:").resource_descriptor() is None
    descriptor = SQLiteKVStore(db_path=str(tmp_path / "kv.db")).resource_descriptor()
    assert descriptor is not None and descriptor.startswith("sqlite://")


def test_auto_registration_writes_topology() -> None:
    placement = InMemoryKVStore()
    routed = _routed(
        placement=placement,
        descriptors={
            "working": "db:5432/memory/public/kv_working",
            "profile": "db:5432/memory/public/kv_profile",
        },
    )
    manager = CompositeStoreManager(kv=routed)
    _register_shard_topology(manager)
    topology = routed.read_topology()
    assert topology["working"].ports == {
        "kv:default": "db:5432/memory/public/kv_working"
    }
    assert topology["profile"].ports == {
        "kv:default": "db:5432/memory/public/kv_profile"
    }
    # 幂等：同样标识再次登记不报错
    _register_shard_topology(manager)


def test_auto_registration_rejects_changed_identity() -> None:
    placement = InMemoryKVStore()
    first = _routed(
        placement=placement,
        descriptors={"working": "db:5432/a", "profile": "db:5432/b"},
    )
    _register_shard_topology(CompositeStoreManager(kv=first))
    second = _routed(
        placement=placement,
        descriptors={"working": "db:5432/moved", "profile": "db:5432/b"},
    )
    with pytest.raises(ValidationError):
        _register_shard_topology(CompositeStoreManager(kv=second))


def test_multi_port_topology_keeps_distinct_labels() -> None:
    """P1：向量 L2/L0/L1 等端口不能互相覆盖登记键。"""

    class _FakeVector:
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        @staticmethod
        def capabilities():
            from jiuwen_memory.storage.capability import StoreCapability

            return frozenset({StoreCapability.SHARD_ROUTED})

        def shard_descriptors(self, kind="vector"):
            return {
                shard: {kind: f"es:9200/index_{self.suffix}"}
                for shard in ("working", "profile")
            }

    routed = _routed(
        placement=InMemoryKVStore(),
        descriptors={"working": "db:a", "profile": "db:b"},
    )
    from jiuwen_memory.storage.store_manager import StorageCapability

    manager = CompositeStoreManager(kv=routed)
    getattr(manager, "_named_stores")[StorageCapability.VECTOR] = {
        "default": _FakeVector("l2"),
        "layers_l0": _FakeVector("l0"),
        "layers_l1": _FakeVector("l1"),
    }
    _register_shard_topology(manager)
    ports = routed.read_topology()["working"].ports
    assert ports["vector:default"] == "es:9200/index_l2"
    assert ports["vector:layers_l0"] == "es:9200/index_l0"
    assert ports["vector:layers_l1"] == "es:9200/index_l1"


def test_partial_kv_descriptors_rejected() -> None:
    """P1：KV 只声明部分分片物理标识时不得静默跳过登记。"""
    class _HalfDescribed:
        @staticmethod
        def capabilities():
            from jiuwen_memory.storage.capability import StoreCapability

            return frozenset({StoreCapability.SHARD_ROUTED, StoreCapability.SHARD_LOCATOR})

        shards = ("working", "profile")

        @staticmethod
        def shard_descriptors(kind="kv"):
            return {"working": {kind: "db:a"}}

        @staticmethod
        def register_topology(descriptors):
            raise AssertionError("must not register incomplete topology")

    class _Manager:
        from jiuwen_memory.storage.store_manager import StorageCapability

        _ports = {"default": _HalfDescribed()}

        def iter_named_ports(self):
            return [
                (self.StorageCapability.KV, name, port)
                for name, port in self._ports.items()
            ]

    with pytest.raises(ValidationError):
        _register_shard_topology(_Manager())


def test_memory_backends_skip_auto_registration() -> None:
    routed = _routed(
        placement=InMemoryKVStore(),
        descriptors={"working": "", "profile": ""},
    )

    class _PlainKV(InMemoryKVStore):
        @staticmethod
        def resource_descriptor() -> None:
            return None

    setattr(
        routed,
        "_stores",
        {name: _PlainKV() for name in ("working", "profile")},
    )
    _register_shard_topology(CompositeStoreManager(kv=routed))
    assert routed.read_topology() == {}


class _MilvusVectorPort:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    @staticmethod
    def capabilities():
        from jiuwen_memory.storage.capability import StoreCapability

        return frozenset({StoreCapability.SHARD_ROUTED})

    def shard_descriptors(self, kind: str = "vector"):
        return {
            shard: {kind: f"{self.prefix}/{shard}"}
            for shard in ("working", "profile")
        }


def _manager_with_milvus(routed, prefix: str):
    return CompositeStoreManager(
        kv=routed, vector={"default": _MilvusVectorPort(prefix)}
    )


def test_legacy_milvus_topology_is_upgraded_in_place() -> None:
    placement = InMemoryKVStore()
    routed = _routed(
        placement=placement,
        descriptors={"working": "db:a", "profile": "db:b"},
    )
    routed.register_topology(
        {
            "working": {
                "kv:default": "db:a",
                "vector:default": "milvus://milvus:19530",
            },
            "profile": {
                "kv:default": "db:b",
                "vector:default": "milvus://milvus:19530",
            },
        }
    )

    _register_shard_topology(
        _manager_with_milvus(routed, "milvus://milvus:19530")
    )

    ports = routed.read_topology()["working"].ports
    assert ports["vector:default"] == "milvus://milvus:19530/working"
    assert ports["kv:default"] == "db:a"


def test_legacy_milvus_topology_rejects_changed_collection() -> None:
    routed = _routed(
        placement=InMemoryKVStore(),
        descriptors={"working": "db:a", "profile": "db:b"},
    )
    routed.register_topology(
        {
            "working": {
                "kv:default": "db:a",
                "vector:default": "milvus://milvus:19530/old",
            },
            "profile": {
                "kv:default": "db:b",
                "vector:default": "milvus://milvus:19530/old",
            },
        }
    )

    with pytest.raises(ValidationError, match="physical identity changed"):
        _register_shard_topology(
            _manager_with_milvus(routed, "milvus://milvus:19530/new")
        )


def test_legacy_milvus_upgrade_tolerates_concurrent_conflict(monkeypatch) -> None:
    routed = _routed(
        placement=InMemoryKVStore(),
        descriptors={"working": "db:a", "profile": "db:b"},
    )
    routed.register_topology(
        {
            "working": {
                "kv:default": "db:a",
                "vector:default": "milvus://milvus:19530",
            },
            "profile": {
                "kv:default": "db:b",
                "vector:default": "milvus://milvus:19530",
            },
        }
    )
    real_update = routed.update_topology

    def conflicting_update(shard, ports):
        # 模拟另一实例已先写入新值，本实例的条件替换随后失败。
        real_update(shard, ports)
        raise ConflictError("concurrent upgrade")

    monkeypatch.setattr(routed, "update_topology", conflicting_update)
    _register_shard_topology(
        _manager_with_milvus(routed, "milvus://milvus:19530")
    )

    assert (
        routed.read_topology()["working"].ports["vector:default"]
        == "milvus://milvus:19530/working"
    )
