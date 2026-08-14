"""RoutingStorage：按 ConfigSource ``storage.active`` 在已预装 Storage 实例间动态选用（F02）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.routing import ActiveRouter, RoutingStorage
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl import CompositeStorage
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore
from jiuwen_memory.storage.types import VectorQuery, VectorRecord

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="o", user="u")


def _unit(unit_id: str, content: str = "c") -> MemoryUnit:
    return MemoryUnit(id=unit_id, scope=_SCOPE, segments=[Segment(content=content)])


def _router(
    instances: dict[str, CompositeStorage],
    cfg: DictConfigSource,
    default: str = "ops",
) -> ActiveRouter:
    return ActiveRouter(
        namespace="storage",
        instances=instances,
        config_source=cfg,
        default_name=default,
    )


def test_routing_storage_switches_by_active() -> None:
    ops = CompositeStorage(kv=InMemoryKVStore())
    compliance = CompositeStorage(kv=InMemoryKVStore())
    cfg = DictConfigSource({"storage.active": "ops"})
    storage = RoutingStorage(_router({"ops": ops, "compliance": compliance}, cfg))

    storage.add(_SCOPE, [_unit("in-ops")])
    assert {u.id for u in storage.list(_SCOPE).items} == {"in-ops"}

    cfg.put("storage.active", "compliance")
    assert storage.list(_SCOPE).items == [], "切换后旧实例数据不可见（无迁移）"
    storage.add(_SCOPE, [_unit("in-compliance")])
    assert {u.id for u in storage.list(_SCOPE).items} == {"in-compliance"}

    cfg.put("storage.active", "ops")
    assert {u.id for u in storage.list(_SCOPE).items} == {"in-ops"}


def test_routing_storage_unknown_active_raises() -> None:
    cfg = DictConfigSource({"storage.active": "ghost"})
    storage = RoutingStorage(
        _router({"ops": CompositeStorage(kv=InMemoryKVStore())}, cfg)
    )
    with pytest.raises(ValidationError, match="ghost"):
        storage.health()


def test_lazy_port_follows_active_after_construct_cache() -> None:
    """模拟 IndexBuilder：构造期缓存 storage.vector，切换 active 后仍打到新实例。"""
    vec_a = InMemoryVectorStore()
    vec_b = InMemoryVectorStore()
    instance_a = CompositeStorage(kv=InMemoryKVStore(), vector=vec_a)
    instance_b = CompositeStorage(kv=InMemoryKVStore(), vector=vec_b)
    cfg = DictConfigSource({"storage.active": "a"})
    storage = RoutingStorage(_router({"a": instance_a, "b": instance_b}, cfg, default="a"))

    # 构造期握端口（与 VectorIndexBuilder / VectorDedup 相同）
    cached_vector = storage.vector
    assert cached_vector is storage.vector, "默认端口代理对象身份应稳定"

    rec_a = VectorRecord(id="va", vector=[1.0, 0.0], metadata={})
    cached_vector.insert(_SCOPE, [rec_a])
    assert instance_a.vector.get(_SCOPE, ["va"]), "active=a 时应写入实例 a"
    assert instance_b.vector.get(_SCOPE, ["va"]) == []

    cfg.put("storage.active", "b")
    rec_b = VectorRecord(id="vb", vector=[0.0, 1.0], metadata={})
    cached_vector.insert(_SCOPE, [rec_b])
    assert instance_b.vector.get(_SCOPE, ["vb"]), "切换后缓存端口须打到实例 b"
    assert instance_a.vector.get(_SCOPE, ["vb"]) == []
    hits = cached_vector.search(_SCOPE, VectorQuery(vector=[0.0, 1.0], top_k=1))
    assert hits and hits[0].id == "vb"


def test_capabilities_follow_active() -> None:
    kv_only = CompositeStorage(kv=InMemoryKVStore())
    with_vector = CompositeStorage(kv=InMemoryKVStore(), vector=InMemoryVectorStore())
    cfg = DictConfigSource({"storage.active": "kv"})
    storage = RoutingStorage(
        _router({"kv": kv_only, "vec": with_vector}, cfg, default="kv")
    )
    assert storage.has_kv() and not storage.has_vector()
    cfg.put("storage.active", "vec")
    assert storage.has_vector()


def test_routing_storage_delegates_domain_to_mock() -> None:
    """领域方法每次 get()，与 Store 级 Routing 同构。"""
    a = MagicMock(name="storage-a")
    b = MagicMock(name="storage-b")
    a.capabilities.return_value = frozenset()
    b.capabilities.return_value = frozenset()
    cfg = DictConfigSource({"storage.active": "a"})
    storage = RoutingStorage(
        ActiveRouter(
            namespace="storage",
            instances={"a": a, "b": b},
            config_source=cfg,
            default_name="a",
        )
    )
    storage.health()
    a.health.assert_called_once()
    cfg.put("storage.active", "b")
    storage.health()
    b.health.assert_called_once()
