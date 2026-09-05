"""CompositeStoreManager 端口/能力/安全 + CompositeDomainStore 领域接口。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import (
    PermissionDeniedError,
    UnsupportedStorageCapabilityError,
    ValidationError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import IndexRemoveMode, KVMemoryListResult
from tests.conftest import make_storage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """具名实例缓存跨测试隔离：producer 级 build_named 用例依赖干净缓存。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


class DenyWritesSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if action in {StorageAction.ADD, StorageAction.UPDATE, StorageAction.DELETE}:
            raise PermissionDeniedError(action.value)


class RecordingKVStore(InMemoryKVStore):
    def __init__(self) -> None:
        super().__init__()
        self.list_extensions: dict[str, str] | None = None
        self.mget_batches: list[list[str]] = []

    def list(self, scope: Scope, **kwargs: Any) -> KVMemoryListResult:
        self.list_extensions = kwargs.get("extensions")
        return super().list(scope, **kwargs)

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        self.mget_batches.append(list(keys))
        return super().mget(scope, keys)


def _unit(scope: Scope, unit_id: str, content: str = "content") -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=scope, segments=[Segment(content=content)])


def test_capabilities_and_ports_have_one_source_of_truth() -> None:
    kv = InMemoryKVStore()
    storage = make_storage(kv=kv)

    assert storage.capabilities() == frozenset({StorageCapability.KV})
    assert storage.has_kv()
    assert not storage.has_vector()
    assert storage.kv().store_type() == kv.store_type()
    assert not storage.kv().security.enabled()
    with pytest.raises(UnsupportedStorageCapabilityError):
        storage.vector()


def test_memory_unit_crud_and_list_preserve_scope_and_count() -> None:
    scope = Scope(org="org", space="space", user="user")
    kv = RecordingKVStore()
    domain_store = make_storage(kv=kv).domain_store()
    first = _unit(scope, "u1", "first")
    second = _unit(scope, "u2", "second")

    domain_store.add(scope, [first, second])
    assert [unit.id for unit in domain_store.get(scope, ["u2", "missing", "u1"])] == [
        "u2",
        "u1",
    ]
    assert [unit.id for unit in domain_store.get(scope, ["u1", "u1"])] == ["u1", "u1"]

    page = domain_store.list(scope, offset=1, limit=1, extensions={"route": "custom"})
    assert page.count == 2
    assert len(page.items) == 1
    assert kv.list_extensions == {"route": "custom"}

    updated = _unit(scope, "u1", "updated")
    domain_store.update(scope, [updated])
    assert domain_store.get(scope, ["u1"])[0].content == "updated"

    domain_store.delete(scope, ["u1"])
    assert domain_store.get(scope, ["u1"]) == []


def test_soft_delete_is_noop_and_body_stays_readable() -> None:
    """SOFT 软删除：无检索索引可移除，CompositeDomainStore 空操作，本体仍可读。"""
    scope = Scope(org="org", space="space", user="user")
    domain_store = make_storage(kv=RecordingKVStore()).domain_store()
    unit = _unit(scope, "u1", "first")
    domain_store.add(scope, [unit])

    domain_store.delete(scope, ["u1"], mode=IndexRemoveMode.SOFT)

    assert domain_store.get(scope, ["u1"]) == [unit]
    assert domain_store.list(scope).count == 1

    domain_store.delete(scope, ["u1"], mode=IndexRemoveMode.HARD)
    assert domain_store.get(scope, ["u1"]) == []


def test_get_reads_truth_source_in_one_deduplicated_batch() -> None:
    scope = Scope(org="org")
    kv = RecordingKVStore()
    domain_store = make_storage(kv=kv).domain_store()
    domain_store.add(scope, [_unit(scope, "u1"), _unit(scope, "u2")])

    # 一次 mget 覆盖去重后的 key；返回按输入顺序展开，重复 id 各自返回。
    assert [unit.id for unit in domain_store.get(scope, ["u2", "u1", "u2"])] == [
        "u2",
        "u1",
        "u2",
    ]
    assert kv.mget_batches == [[memory_key("u2"), memory_key("u1")]]

    # mget 任一 key 缺失即抛 NotFoundError，由 _get_units 回退逐条并跳过缺失。
    kv.mget_batches.clear()
    assert [unit.id for unit in domain_store.get(scope, ["u1", "missing"])] == ["u1"]
    assert kv.mget_batches == [[memory_key("u1"), memory_key("missing")]]


def test_add_rejects_unit_owned_by_another_scope() -> None:
    requested = Scope(org="org", space="one")
    other = Scope(org="org", space="two")
    domain_store = make_storage(kv=InMemoryKVStore()).domain_store()

    with pytest.raises(ValidationError):
        domain_store.add(requested, [_unit(other, "u1")])


def test_common_security_guards_domain_and_direct_port_operations() -> None:
    scope = Scope(org="org")
    storage = make_storage(kv=InMemoryKVStore(), security=DenyWritesSecurity())
    domain_store = storage.domain_store()

    with pytest.raises(PermissionDeniedError):
        domain_store.add(scope, [_unit(scope, "u1")])
    with pytest.raises(PermissionDeniedError):
        storage.kv().insert(scope, "/raw", b"value")

    assert domain_store.get(scope, ["missing"]) == []


def test_health_checks_storage_security_and_declared_store() -> None:
    storage = make_storage(kv=InMemoryKVStore())

    assert storage.health() is None


def test_store_manager_producer_builds_named_composite_with_configured_ports() -> None:
    register_backends()
    context = AssemblyContext.from_dict(
        {
            # 具名 manager 的召回路装配经 globals.store_manager 指名回取本实例
            #（预注册缓存命中）；关 graph 避免无 graph 端口时装配失败。
            "globals": {"store_manager": "main", "graph_enabled": False},
            "kv_store": {"truth": "memory"},
            "vector_store": {"semantic": "memory"},
            "store_manager": {
                "main": {
                    "target": "composite",
                    "params": {"kv_store": "truth", "vector_store": "semantic"},
                }
            },
        }
    )

    storage = StoreManagerProducer.build_named("main", context)

    assert isinstance(storage, CompositeStoreManager)
    assert storage.capabilities() == frozenset(
        {StorageCapability.KV, StorageCapability.VECTOR}
    )
    assert StoreManagerProducer.build_named("main", context) is storage


def test_store_manager_producer_rejects_unknown_retrieval_pipeline() -> None:
    register_backends()

    with pytest.raises(ValidationError, match="preferred_retrieval_pipeline"):
        StoreManagerProducer.build(
            "composite",
            {"kv_store": {"target": "memory"}, "preferred_retrieval_pipeline": "unknown"},
            AssemblyContext(),
        )


def test_store_manager_missing_kv_store_shares_named_default() -> None:
    """kv_store 键缺失 → 共享 kv_store.default 具名实例，不匿名新建（防真源分裂）。"""
    register_backends()
    ctx = AssemblyContext.from_dict({"kv_store": {"default": "memory"}})

    manager = StoreManagerProducer.build("composite", {}, ctx)

    assert manager._stores[StorageCapability.KV] is KvProducer.build_named("default", ctx)


def test_store_manager_missing_kv_store_without_default_fails() -> None:
    """kv_store 键缺失且未声明 kv_store.default → build_named 抛 ValidationError。"""
    register_backends()

    with pytest.raises(ValidationError, match="kv_store"):
        StoreManagerProducer.build("composite", {}, AssemblyContext())
