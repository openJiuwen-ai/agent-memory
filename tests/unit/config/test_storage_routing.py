"""RoutingStoreManager/RoutingDomainStore：按 ``store_manager.active`` 动态选用（F02/F08）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.common.type_def.entity import EntityStoreFilters
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.routing import ActiveRouter, RoutingStoreManager
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from tests.conftest import make_storage

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="o", user="u")


def _unit(unit_id: str, content: str = "c") -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=_SCOPE, segments=[Segment(content=content)])


def _router(
    instances: dict[str, CompositeStoreManager],
    cfg: DictConfigSource,
    default: str = "ops",
) -> ActiveRouter:
    return ActiveRouter(
        namespace="store_manager",
        instances=instances,
        config_source=cfg,
        default_name=default,
    )


def test_routing_store_manager_switches_by_active() -> None:
    ops = make_storage(kv=InMemoryKVStore())
    compliance = make_storage(kv=InMemoryKVStore())
    cfg = DictConfigSource({"store_manager.active": "ops"})
    storage = RoutingStoreManager(_router({"ops": ops, "compliance": compliance}, cfg))

    domain = storage.domain_store()
    domain.add(_SCOPE, [_unit("in-ops")])
    assert {u.id for u in domain.list(_SCOPE).items} == {"in-ops"}

    cfg.put("store_manager.active", "compliance")
    assert domain.list(_SCOPE).items == [], "切换后旧实例数据不可见（无迁移）"
    domain.add(_SCOPE, [_unit("in-compliance")])
    assert {u.id for u in domain.list(_SCOPE).items} == {"in-compliance"}

    cfg.put("store_manager.active", "ops")
    assert {u.id for u in domain.list(_SCOPE).items} == {"in-ops"}


def test_routing_store_manager_unknown_active_raises() -> None:
    cfg = DictConfigSource({"store_manager.active": "ghost"})
    storage = RoutingStoreManager(
        _router({"ops": make_storage(kv=InMemoryKVStore())}, cfg)
    )
    with pytest.raises(ValidationError, match="ghost"):
        storage.health()


def test_lazy_port_follows_active_after_construct_cache() -> None:
    """模拟 IndexBuilder：构造期缓存 manager.vector()，切换 active 后仍打到新实例。"""
    vec_a = InMemoryVectorStore()
    vec_b = InMemoryVectorStore()
    instance_a = CompositeStoreManager(kv=InMemoryKVStore(), vector=vec_a)
    instance_b = CompositeStoreManager(kv=InMemoryKVStore(), vector=vec_b)
    cfg = DictConfigSource({"store_manager.active": "a"})
    storage = RoutingStoreManager(
        _router({"a": instance_a, "b": instance_b}, cfg, default="a")
    )

    # 构造期握端口（与 VectorIndexBuilder / VectorDedup 相同）
    cached_vector = storage.vector()
    assert cached_vector is storage.vector(), "默认端口代理对象身份应稳定"

    rec_a = VectorRecord(id="va", vector=[1.0, 0.0], metadata={})
    cached_vector.insert(_SCOPE, [rec_a])
    assert instance_a.vector().get(_SCOPE, ["va"]), "active=a 时应写入实例 a"
    assert instance_b.vector().get(_SCOPE, ["va"]) == []

    cfg.put("store_manager.active", "b")
    rec_b = VectorRecord(id="vb", vector=[0.0, 1.0], metadata={})
    cached_vector.insert(_SCOPE, [rec_b])
    assert instance_b.vector().get(_SCOPE, ["vb"]), "切换后缓存端口须打到实例 b"
    assert instance_a.vector().get(_SCOPE, ["vb"]) == []
    hits = cached_vector.search(_SCOPE, VectorQuery(vector=[0.0, 1.0], top_k=1))
    assert hits and hits[0].id == "vb"


def test_capabilities_follow_active() -> None:
    kv_only = CompositeStoreManager(kv=InMemoryKVStore())
    with_vector = CompositeStoreManager(kv=InMemoryKVStore(), vector=InMemoryVectorStore())
    cfg = DictConfigSource({"store_manager.active": "kv"})
    storage = RoutingStoreManager(
        _router({"kv": kv_only, "vec": with_vector}, cfg, default="kv")
    )
    assert storage.has_kv() and not storage.has_vector()
    cfg.put("store_manager.active", "vec")
    assert storage.has_vector()


def test_entity_port_follows_active() -> None:
    """ENTITY 端口（F07-D 第七席）与其余六类同样随 active 切换，走同一 _lazy_port 机制。"""
    from tests.unit.construction.test_entity_linker import InMemoryEntityStore

    ent = InMemoryEntityStore()
    kv_only = CompositeStoreManager(kv=InMemoryKVStore())
    with_entity = CompositeStoreManager(kv=InMemoryKVStore(), entity=ent)
    cfg = DictConfigSource({"store_manager.active": "kv"})
    storage = RoutingStoreManager(
        _router({"kv": kv_only, "ent": with_entity}, cfg, default="kv")
    )

    assert not storage.has_entity()
    # 构造期握端口（与 KeywordRecaller / EntityLinkService 相同）
    cached_entity = storage.entity()
    assert cached_entity is storage.entity(), "默认端口代理对象身份应稳定"

    cfg.put("store_manager.active", "ent")
    assert storage.has_entity()
    # 缓存的惰性端口在切换后解析到新实例；entity 首参是 space_id 而非 Scope，
    # _LazyStorePort 透明转发不做签名假设
    cached_entity.ensure_index()
    assert cached_entity.find_by_entity_text_hash(
        "space", ("h1",), filters=EntityStoreFilters(actor_id="u1")
    ) == []


def test_routing_store_manager_delegates_management_to_mock() -> None:
    """管理面方法每次 get()，与 Store 级 Routing 同构。"""
    a = MagicMock(name="store-manager-a")
    b = MagicMock(name="store-manager-b")
    a.capabilities.return_value = frozenset()
    b.capabilities.return_value = frozenset()
    cfg = DictConfigSource({"store_manager.active": "a"})
    storage = RoutingStoreManager(
        ActiveRouter(
            namespace="store_manager",
            instances={"a": a, "b": b},
            config_source=cfg,
            default_name="a",
        )
    )
    storage.health()
    a.health.assert_called_once()
    cfg.put("store_manager.active", "b")
    storage.health()
    b.health.assert_called_once()
