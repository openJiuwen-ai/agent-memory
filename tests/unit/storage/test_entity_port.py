# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ENTITY 端口（StorageCapability 第七席，F07-D）：能力发现、命名端口、授权代理、health、装配。

F07-D 把 EntityStore 从旁路装配（两侧消费方各自 ``EntityStoreProducer.dep``）收进
StoreManager 管理。本文件覆盖端口侧契约；消费方经端口取实例的接线由
``tests/unit/construction/test_hybrid_entity_wiring.py`` 覆盖。

桩不复用 ``tests/unit/construction/test_entity_linker.py::InMemoryEntityStore``：那是
行为完整的内存实现，而这里要断言的是「哪个方法被调用、带什么 space_id/filters、授权
走了哪些 action」，需要 spy；storage 层测试反向 import construction 层测试模块也是
层级倒置。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    PermissionDeniedError,
    UnsupportedStorageCapabilityError,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.entity import (
    EntityBatchResult,
    EntityOperation,
    EntityOpType,
    EntityRecord,
    EntityStoreFilters,
)
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.entity_store import EntityStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.security import StorageAccessContext, StorageAction, StorageSecurity
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManagerProducer
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """具名实例缓存跨测试隔离：producer 级 build_named 用例依赖干净缓存。"""
    Factory.reset_all()
    yield
    Factory.reset_all()


class _RecordingEntityStore(EntityStore):
    """记录调用的 ENTITY 端口 spy；health 可切换失败以覆盖聚合。"""

    def __init__(self, *, healthy: bool = True) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._healthy = healthy

    def store_type(self) -> StoreType:
        return StoreType.ENTITY

    def health(self) -> None:
        if not self._healthy:
            raise BackendError("entity backend down")

    def ensure_index(self) -> None:
        self.calls.append(("ensure_index", (), {}))

    def find_by_entity_text_hash(
        self,
        space_id: str,
        entity_text_hashes: tuple[str, ...],
        *,
        filters: EntityStoreFilters,
        limit: int = 500,
    ) -> list[EntityRecord]:
        self.calls.append(
            ("find_by_entity_text_hash", (space_id, entity_text_hashes), {"filters": filters})
        )
        return []

    def find_by_linked_memory_id(
        self,
        space_id: str,
        memory_id: str,
        *,
        filters: EntityStoreFilters,
    ) -> list[EntityRecord]:
        self.calls.append(
            ("find_by_linked_memory_id", (space_id, memory_id), {"filters": filters})
        )
        return []

    def execute_operations(
        self, space_id: str, operations: list[EntityOperation]
    ) -> EntityBatchResult:
        self.calls.append(("execute_operations", (space_id, operations), {}))
        return EntityBatchResult(successful_ids=[], failed_ids=[])


class _RecordingSecurity(StorageSecurity):
    """记录每次 authorize 的四元组，供 action / scope / resource 断言。"""

    def __init__(self) -> None:
        self.seen: list[tuple[Any, Scope, StorageAction, str]] = []

    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        self.seen.append((access, scope, action, resource))

    @property
    def actions(self) -> list[StorageAction]:
        return [action for _, _, action, _ in self.seen]


class _DenyWritesSecurity(StorageSecurity):
    def authorize(
        self,
        access: StorageAccessContext | None,
        scope: Scope,
        action: StorageAction,
        resource: str,
    ) -> None:
        if action in {StorageAction.ADD, StorageAction.UPDATE, StorageAction.DELETE}:
            raise PermissionDeniedError(f"writes denied on {resource}")


def _op(op_type: EntityOpType) -> EntityOperation:
    return EntityOperation(type=op_type, record_id="e1")


# ---------------------------------------------------------------------------
# 能力发现与端口暴露
# ---------------------------------------------------------------------------


def test_entity_capability_and_port_exposed() -> None:
    stub = _RecordingEntityStore()
    manager = CompositeStoreManager(kv=InMemoryKVStore(), entity=stub)

    assert manager.capabilities() == frozenset(
        {StorageCapability.KV, StorageCapability.ENTITY}
    )
    assert manager.has_entity()
    # 端口返回的是授权代理而非裸实例
    assert manager.entity() is not stub
    assert manager.entity().store_type() is StoreType.ENTITY
    # 同名多次取用身份稳定（消费方常在 __init__ 里缓存端口引用）
    assert manager.entity() is manager.entity()


def test_entity_absent_capability_and_error() -> None:
    manager = CompositeStoreManager(kv=InMemoryKVStore())

    assert StorageCapability.ENTITY not in manager.capabilities()
    assert not manager.has_entity()
    assert not manager.has_entity("aux")
    with pytest.raises(UnsupportedStorageCapabilityError):
        manager.entity()


def test_named_entity_ports_truth_table() -> None:
    default_stub = _RecordingEntityStore()
    aux_stub = _RecordingEntityStore()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=default_stub, entity_ports={"aux": aux_stub}
    )

    assert manager.has_entity()
    assert manager.has_entity("aux")
    assert not manager.has_entity("missing")

    manager.entity("aux").ensure_index()
    assert [name for name, _, _ in aux_stub.calls] == ["ensure_index"]
    assert default_stub.calls == []


def test_entity_ports_drop_none() -> None:
    """builder 返 None 的具名实例不得进端口表——端口值非 None 是 manager 不变量。"""
    manager = CompositeStoreManager(kv=InMemoryKVStore(), entity_ports={"broken": None})

    assert not manager.has_entity("broken")
    assert StorageCapability.ENTITY not in manager.capabilities()
    assert manager.health() is None  # health 遍历不会踩到 None.security


# ---------------------------------------------------------------------------
# 授权代理：action 映射与 scope 近似
# ---------------------------------------------------------------------------


def test_entity_proxy_maps_query_methods_to_search() -> None:
    security = _RecordingSecurity()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=_RecordingEntityStore(), security=security
    )
    filters = EntityStoreFilters(actor_id="u1")

    manager.entity().find_by_entity_text_hash("s1", ("h1",), filters=filters)
    manager.entity().find_by_linked_memory_id("s1", "m1", filters=filters)

    assert security.actions == [StorageAction.SEARCH, StorageAction.SEARCH]
    for _, scope, _, resource in security.seen:
        assert resource == "entity"
        # scope 是有损近似：只有 space / user 两段有意义
        assert scope == Scope(space="s1", user="u1")


def test_entity_proxy_maps_ensure_index_to_admin() -> None:
    security = _RecordingSecurity()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=_RecordingEntityStore(), security=security
    )

    manager.entity().ensure_index()

    assert security.actions == [StorageAction.ADMIN]
    assert security.seen[0][1] == Scope()  # 无 space_id 入参


def test_entity_proxy_derives_actions_from_batch() -> None:
    """execute_operations 按 batch 内 op 类型派生动作集（UPDATE 去重），而非固定 ADMIN。"""
    security = _RecordingSecurity()
    stub = _RecordingEntityStore()
    manager = CompositeStoreManager(kv=InMemoryKVStore(), entity=stub, security=security)

    manager.entity().execute_operations(
        "s1",
        [
            _op(EntityOpType.INSERT),
            _op(EntityOpType.LINK),
            _op(EntityOpType.UNLINK_UPDATE),
            _op(EntityOpType.DELETE),
        ],
    )

    # LINK 与 UNLINK_UPDATE 同归 UPDATE，去重后三个动作
    assert set(security.actions) == {
        StorageAction.ADD,
        StorageAction.UPDATE,
        StorageAction.DELETE,
    }
    assert len(security.actions) == 3
    # execute_operations 无 filters 参数，故 user 恒空
    assert security.seen[0][1] == Scope(space="s1")


def test_entity_proxy_empty_batch_skips_authorize() -> None:
    """空 batch 不执行任何动作，故零次 authorize，但方法仍被委托。"""
    security = _RecordingSecurity()
    stub = _RecordingEntityStore()
    manager = CompositeStoreManager(kv=InMemoryKVStore(), entity=stub, security=security)

    manager.entity().execute_operations("s1", [])

    assert security.actions == []
    assert [name for name, _, _ in stub.calls] == ["execute_operations"]


def test_entity_port_denies_writes_but_allows_query() -> None:
    stub = _RecordingEntityStore()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=stub, security=_DenyWritesSecurity()
    )

    with pytest.raises(PermissionDeniedError):
        manager.entity().execute_operations("s1", [_op(EntityOpType.INSERT)])
    # 授权发生在委托之前：被拒的调用不得触达后端
    assert stub.calls == []

    manager.entity().find_by_entity_text_hash(
        "s1", ("h1",), filters=EntityStoreFilters(actor_id="u1")
    )
    assert [name for name, _, _ in stub.calls] == ["find_by_entity_text_hash"]


# ---------------------------------------------------------------------------
# health 聚合
# ---------------------------------------------------------------------------


def test_health_includes_entity_port() -> None:
    healthy = CompositeStoreManager(kv=InMemoryKVStore(), entity=_RecordingEntityStore())
    assert healthy.health() is None

    broken = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=_RecordingEntityStore(healthy=False)
    )
    with pytest.raises(BackendError):
        broken.health()


def test_health_dedupes_shared_entity_instance() -> None:
    """default 与命名端口指向同一实例时只探一次（health 按 id() 去重）。"""

    class _CountingEntityStore(_RecordingEntityStore):
        def __init__(self) -> None:
            super().__init__()
            self.health_calls = 0

        def health(self) -> None:
            self.health_calls += 1

    shared = _CountingEntityStore()
    manager = CompositeStoreManager(
        kv=InMemoryKVStore(), entity=shared, entity_ports={"aux": shared}
    )

    manager.health()

    assert shared.health_calls == 1


# ---------------------------------------------------------------------------
# 装配链路（不连真实 ES：ES store 构造期零 IO，client 是惰性 property）
# ---------------------------------------------------------------------------


def _ctx(entity_ns: dict[str, Any] | None, manager_params: dict[str, Any] | None = None) -> Any:
    raw: dict[str, Any] = {
        "globals": {"store_manager": "main", "vector_enabled": False, "graph_enabled": False},
        "kv_store": {"truth": "memory"},
        "store_manager": {
            "main": {
                "target": "composite",
                "params": {"kv_store": "truth", **(manager_params or {})},
            }
        },
    }
    if entity_ns is not None:
        raw["entity_store"] = entity_ns
    return AssemblyContext.from_dict(raw)


def _es(**params: Any) -> dict[str, Any]:
    return {"target": "elasticsearch", "params": params}


def test_store_manager_builds_entity_port_from_namespace_default() -> None:
    """声明 entity_store.default 即成为 manager 的 ENTITY 默认端口（三级解析第二级）。"""
    register_backends()
    ctx = _ctx({"default": _es(hosts="http://es:9200", index="ents")})

    manager = StoreManagerProducer.build_named("main", ctx)

    assert manager.has_entity()
    assert StorageCapability.ENTITY in manager.capabilities()
    # 不调 health()：那会触发 client.ping()


def test_entity_store_without_hosts_degrades() -> None:
    """hosts 未配 → ES builder 返 None → 无 ENTITY 能力（装配期降级，不报错）。"""
    register_backends()
    ctx = _ctx({"default": _es(index="ents")})

    manager = StoreManagerProducer.build_named("main", ctx)

    assert not manager.has_entity()
    assert StorageCapability.ENTITY not in manager.capabilities()
    assert manager.health() is None


def test_no_entity_namespace_means_no_capability() -> None:
    register_backends()
    manager = StoreManagerProducer.build_named("main", _ctx(None))

    assert not manager.has_entity()
    assert StorageCapability.ENTITY not in manager.capabilities()


def test_named_entity_port_without_hosts_is_dropped() -> None:
    """具名实例 builder 返 None 时该端口被丢弃，且不影响 default 与 health。"""
    register_backends()
    ctx = _ctx(
        {
            "default": _es(hosts="http://es:9200", index="ents"),
            "aux": _es(index="ents_aux"),  # 无 hosts
        }
    )

    manager = StoreManagerProducer.build_named("main", ctx)

    assert manager.has_entity()
    assert not manager.has_entity("aux")


def test_entity_port_params_reference_wins() -> None:
    """store_manager params 显式引用优先于 entity_store.default（三级解析第一级）。

    构造判别式：``default`` 缺 hosts（builder 返 None），``aux`` 有 hosts。若 params
    引用生效则 ENTITY 能力存在；若错误地走了第二级 default 兜底则能力缺失。
    """
    register_backends()
    ctx = _ctx(
        {
            "default": _es(index="ents_default"),  # 无 hosts → builder 返 None
            "aux": _es(hosts="http://es-aux:9200", index="ents_aux"),
        },
        manager_params={"entity_store": "aux"},
    )

    manager = StoreManagerProducer.build_named("main", ctx)

    assert manager.has_entity()  # 解析到 aux，而非返 None 的 default
    assert StorageCapability.ENTITY in manager.capabilities()


def test_deploy_shaped_config_assembles_entity_port() -> None:
    """按 deploy config 的形状（顶层 entity_store 段，不动 store_manager）能装出端口。"""
    register_backends()
    ctx = _ctx(
        {
            "default": _es(
                hosts="http://elasticsearch:9200",
                index="memory_entities",
                ssl_verify=False,
                ssl_ca_cert="",
            )
        }
    )

    manager = StoreManagerProducer.build_named("main", ctx)

    assert manager.has_entity()
