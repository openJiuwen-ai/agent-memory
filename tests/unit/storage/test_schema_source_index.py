# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Source lookup equivalence, maintenance and failures on memory and Redis Lua."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import (
    BackendError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from jiuwen_memory.common.memory_sources import sources
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.config.config_source_impl.dict_config_source import DictConfigSource
from jiuwen_memory.config.routing import ActiveRouter, RoutingKVStore
from jiuwen_memory.construction.evolver_impl.schema_update import SchemaUpdateCoordinator
from jiuwen_memory.construction.source_update import SourceExtraction
from jiuwen_memory.control import SpaceSpec
from jiuwen_memory.control.space_impl.kv_space_manager import KVSpaceManager
from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.redis_kv import RedisKVStore
from jiuwen_memory.storage.kv_impl.redis_schema_source_index import _WRITE as WRITE_SCRIPT
from jiuwen_memory.storage.security import StorageAction, StorageSecurity
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager

pytestmark = pytest.mark.unit
SCOPE = Scope(org="index-test", space="workspace", user="u", agent="a", session="s")


@pytest.fixture(params=["memory", "redis", "redis_real"])
def indexed(request, monkeypatch):
    if request.param == "memory":
        store = InMemoryKVStore(schema_source_index_enabled=True)
        yield store, None
        return
    if request.param == "redis_real":
        url = os.getenv("SCHEMA_INDEX_TEST_REDIS_URL")
        if not url:
            pytest.skip("set SCHEMA_INDEX_TEST_REDIS_URL to a dedicated disposable Redis database")
        import redis

        client = redis.Redis.from_url(url)
        client.flushdb()
    else:
        import fakeredis

        client = fakeredis.FakeRedis()
        # fakeredis implements Lua/sets but does not implement CONFIG GET.
        monkeypatch.setattr(client, "config_get", lambda _: {"maxmemory-policy": "noeviction"})
    monkeypatch.setattr(RedisKVStore, "client", property(lambda _: client))
    store = RedisKVStore(schema_source_index_enabled=True)
    store.rebuild_schema_source_index(SCOPE)
    try:
        yield store, client
    finally:
        if request.param == "redis_real":
            client.flushdb()
        client.close()


def property_unit(uid="p", *, provenance=None, source_ref="s1", scope=SCOPE):
    return MemoryUnit(
        id=uid, scope=scope, segments=[Segment(content="小明在 A 公司工作")],
        provenance=provenance if provenance is not None else ["s1", "s2", "s1"],
        source_ref=source_ref, system_metadata={"extraction_mode": "schema"},
    )


def put(store, unit):
    store.insert(unit.scope, memory_key(unit.id), dumps(unit))


def ids(store, sid, scope=SCOPE):
    rows = store.get_schema_properties_by_source(scope, sid)
    assert rows is not None
    return {loads(raw).id for _, raw in rows}


def test_all_sources_and_direct_property_crud(indexed):
    store, _ = indexed
    unit = property_unit(source_ref="ignored")
    put(store, unit)
    assert ids(store, "s1") == ids(store, "s2") == {"p"}
    assert ids(store, "ignored") == set()
    with pytest.raises(ConflictError):
        put(store, property_unit(provenance=["s3"]))
    assert ids(store, "s1") == {"p"}
    unit.provenance = ["s2", "s3"]
    store.update(SCOPE, memory_key(unit.id), dumps(unit))
    assert ids(store, "s1") == set()
    assert ids(store, "s2") == ids(store, "s3") == {"p"}
    unit.provenance, unit.source_ref = [], "fallback"
    store.update(SCOPE, memory_key(unit.id), dumps(unit))
    assert ids(store, "s2") == ids(store, "s3") == set()
    assert ids(store, "fallback") == {"p"}
    unit.system_metadata.clear()
    store.update(SCOPE, memory_key(unit.id), dumps(unit))
    assert ids(store, "fallback") == set()
    store.delete(SCOPE, memory_key(unit.id))
    store.delete(SCOPE, memory_key(unit.id))
    with pytest.raises(NotFoundError):
        store.update(SCOPE, memory_key(unit.id), dumps(unit))


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_full_scope_isolation_and_clear(indexed, dimension):
    store, _ = indexed
    other = replace(SCOPE, **{dimension: "other"})
    store.rebuild_schema_source_index(other)
    put(store, property_unit())
    put(store, property_unit(scope=other, provenance=["other-source"]))
    assert ids(store, "s1", other) == set()
    assert ids(store, "other-source") == set()
    store.clear_schema_source_index(other)
    assert store.get_schema_properties_by_source(other, "other-source") is None
    assert ids(store, "s1") == {"p"}
    assert store.get(other, memory_key("p"))  # Clearing an index must not delete truth.


def test_lookup_equivalent_to_scan_without_full_scan(indexed, monkeypatch):
    store, client = indexed
    for i in range(150):
        unit = property_unit(str(i), provenance=[f"source-{i}"])
        put(store, unit)
    put(store, property_unit("expired", provenance=["source-1"]))
    for unit in (property_unit("history"), property_unit("deleted")):
        unit.lifecycle = LifecycleState.SUPERSEDED
        put(store, unit)
    expected = set()
    for key, raw in store.scan(SCOPE, "/memory/"):
        if "s1" in sources(loads(raw)):
            expected.add(key)
    fail_scan = Mock(side_effect=AssertionError("ready index must not scan"))
    monkeypatch.setattr(store, "scan", fail_scan)
    if client is not None:
        monkeypatch.setattr(client, "scan_iter", fail_scan)
    assert {key for key, _ in store.get_schema_properties_by_source(SCOPE, "s1")} == expected
    assert ids(store, "unknown") == set()
    fail_scan.assert_not_called()


def test_ttl_missing_body_does_not_drop_other_properties(indexed):
    store, _ = indexed
    store.insert(SCOPE, memory_key("expired"), dumps(property_unit("expired")), ttl=0.01)
    put(store, property_unit("live"))
    time.sleep(0.03)
    assert ids(store, "s1") == {"live"}
    put(store, property_unit("expired", provenance=["new-source"]))
    assert ids(store, "s1") == {"live"}
    assert ids(store, "new-source") == {"expired"}


def test_cleared_index_stays_unready_until_explicit_rebuild(indexed):
    store, _ = indexed
    put(store, property_unit())
    store.clear_schema_source_index(SCOPE)
    put(store, property_unit("new", provenance=["s3"]))
    assert store.get_schema_properties_by_source(SCOPE, "s3") is None
    # Valid non-MemoryUnit records do not create relationships.
    store.insert(SCOPE, "/memory/legacy", b'[]')
    store.rebuild_schema_source_index(SCOPE)
    assert ids(store, "s1") == {"p"}
    assert ids(store, "s3") == {"new"}
    assert all(key.startswith("/memory/") for key, _ in store.scan(SCOPE))
    assert store.scopes() == [SCOPE]


def test_failed_backfill_keeps_scan_fallback(indexed, monkeypatch):
    store, client = indexed
    put(store, property_unit())
    module = "redis_schema_source_index" if client is not None else "in_memory_kv_store"
    with monkeypatch.context() as patch:
        patch.setattr(
            f"jiuwen_memory.storage.kv_impl.{module}.property_sources",
            Mock(side_effect=ValueError("bad stored record")),
        )
        with pytest.raises((ValueError, BackendError), match="bad stored record"):
            store.rebuild_schema_source_index(SCOPE)
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    store.rebuild_schema_source_index(SCOPE)
    assert ids(store, "s1") == {"p"}


def test_coordinator_uses_complete_index_and_unready_fallback(indexed, monkeypatch):
    store, _ = indexed
    source = MemoryUnit(id="s1", scope=SCOPE, segments=[Segment(content="old")])
    updated = replace(source, segments=[Segment(content="new")])
    put(store, source)
    put(store, property_unit(provenance=["s1"]))
    coordinator = SchemaUpdateCoordinator(store, lambda _: SourceExtraction([], []))
    scan = Mock(wraps=store.scan)
    monkeypatch.setattr(store, "scan", scan)
    plan = coordinator.prepare(source, updated, mode="overwrite")
    scan.assert_not_called()
    assert any(c.before.id == "p" and c.after is None for c in plan.changes)
    store.clear_schema_source_index(SCOPE)
    plan = coordinator.prepare(source, updated, mode="overwrite")
    scan.assert_called_once_with(SCOPE, "/memory/")
    assert any(c.before.id == "p" and c.after is None for c in plan.changes)


def test_space_delete_cleans_internal_relations_including_expired_bodies(indexed):
    store, client = indexed
    manager = KVSpaceManager(CompositeStoreManager(kv=store))
    manager.create(SpaceSpec(org=SCOPE.org, space=SCOPE.space))
    store.insert(SCOPE, memory_key("expired"), dumps(property_unit("expired")), ttl=0.01)
    time.sleep(0.03)
    manager.delete(SCOPE.org, SCOPE.space)
    assert store.scan(SCOPE) == []
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    if client is not None:
        assert not list(client.scan_iter(match="__jiuwen_schema_sources_v1__/*"))


def test_concurrent_property_writes_do_not_lose_set_members(indexed):
    store, _ = indexed
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: put(store, property_unit(str(i))), range(24)))
    assert ids(store, "s1") == {str(i) for i in range(24)}
    assert ids(store, "s2") == {str(i) for i in range(24)}


def test_redis_old_data_requires_backfill(indexed):
    store, client = indexed
    if client is None:
        pytest.skip("Redis persistence migration")
    store.clear_schema_source_index(SCOPE)
    old_store = RedisKVStore()
    put(old_store, property_unit())
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    store.rebuild_schema_source_index(SCOPE)
    assert ids(store, "s1") == ids(store, "s2") == {"p"}
    assert old_store.scan(SCOPE) == store.scan(SCOPE)


def test_redis_partial_script_failure_invalidates_and_does_not_auto_recover(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis script failure")
    put(store, property_unit())
    unit = property_unit(provenance=["new-source"])
    broken = WRITE_SCRIPT.replace(
        "redis.call('SET', KEYS[1], ARGV[2])",
        "redis.call('SET', KEYS[1], ARGV[2]); return redis.error_reply('injected failure')",
    )
    with monkeypatch.context() as patch:
        patch.setattr("jiuwen_memory.storage.kv_impl.redis_schema_source_index._WRITE", broken)
        with pytest.raises(BackendError, match="injected failure"):
            store.update(SCOPE, memory_key("p"), dumps(unit))
    assert loads(store.get(SCOPE, memory_key("p"))).provenance == ["new-source"]
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    store.update(SCOPE, memory_key("p"), dumps(unit))
    assert store.get_schema_properties_by_source(SCOPE, "new-source") is None
    store.rebuild_schema_source_index(SCOPE)
    assert ids(store, "s1") == set()
    assert ids(store, "new-source") == {"p"}


def test_redis_concurrent_provenance_change_retries_old_edges(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis CAS")
    put(store, property_unit())
    original = client.eval
    first = True

    def interleave(script, *args):
        nonlocal first
        if first:
            first = False
            store.update(SCOPE, memory_key("p"), dumps(property_unit(provenance=["interim"])))
        return original(script, *args)

    monkeypatch.setattr(client, "eval", interleave)
    store.update(SCOPE, memory_key("p"), dumps(property_unit(provenance=["final"])))
    assert ids(store, "s1") == ids(store, "s2") == ids(store, "interim") == set()
    assert ids(store, "final") == {"p"}


def test_redis_refuses_eviction_policy(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis eviction policy")
    monkeypatch.setattr(client, "config_get", lambda _: {"maxmemory-policy": "allkeys-lru"})
    with pytest.raises(ValidationError, match="noeviction"):
        store.rebuild_schema_source_index(SCOPE)


def test_redis_binds_client_once_and_does_not_cache_readiness(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis client binding")
    import fakeredis

    other = fakeredis.FakeRedis()
    put(store, property_unit())
    select_client = Mock(side_effect=[client, other])
    monkeypatch.setattr(RedisKVStore, "client", property(lambda _: select_client()))
    assert ids(store, "s1") == {"p"}
    assert select_client.call_count == 1
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    assert select_client.call_count == 2
    other.close()


def test_redis_readiness_change_during_lookup_is_not_empty_success(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis read interleaving")
    put(store, property_unit())
    original = client.smembers

    def clear_during_read(key):
        store.clear_schema_source_index(SCOPE)
        return original(key)

    monkeypatch.setattr(client, "smembers", clear_during_read)
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None


def test_redis_concurrent_write_prevents_backfill_publication(indexed, monkeypatch):
    store, client = indexed
    if client is None:
        pytest.skip("Redis backfill interleaving")
    put(store, property_unit())
    original = client.eval

    def write_before_publish(script, *args):
        if script != WRITE_SCRIPT:
            unit = property_unit(provenance=["new-source"])
            store.update(SCOPE, memory_key("p"), dumps(unit))
        return original(script, *args)

    monkeypatch.setattr(client, "eval", write_before_publish)
    with pytest.raises(ConflictError, match="concurrent write"):
        store.rebuild_schema_source_index(SCOPE)
    assert store.get_schema_properties_by_source(SCOPE, "new-source") is None


def test_redis_rejects_submillisecond_expiry_without_writing(indexed):
    store, client = indexed
    if client is None:
        pytest.skip("Redis millisecond expiry")
    with pytest.raises(BackendError, match="millisecond"):
        store.insert(SCOPE, memory_key("p"), dumps(property_unit()), ttl=0.0001)
    assert not store.exists(SCOPE, memory_key("p"))
    assert ids(store, "s1") == set()


def test_disabled_index_does_not_parse_or_add_redis_io(monkeypatch):
    monkeypatch.setattr(
        "jiuwen_memory.storage.kv_impl.in_memory_kv_store.property_sources",
        Mock(side_effect=AssertionError("disabled index must not project")),
    )
    for store in (InMemoryKVStore(), RedisKVStore()):
        client = Mock()
        client.set.return_value = True
        monkeypatch.setattr(RedisKVStore, "client", property(lambda _: client))
        store.insert(SCOPE, "/memory/plain", b"not JSON")
        store.update(SCOPE, "/memory/plain", b"still not JSON")
        store.delete(SCOPE, "/memory/plain")
        store.clear_schema_source_index(SCOPE)
        assert store.get_schema_properties_by_source(SCOPE, "s1") is None
        if isinstance(store, RedisKVStore):
            assert [call[0] for call in client.mock_calls] == ["set", "set", "delete"]


def test_routing_binds_candidates_and_bodies_to_one_backend():
    a = InMemoryKVStore(schema_source_index_enabled=True)
    b = InMemoryKVStore(schema_source_index_enabled=True)
    put(a, property_unit("from-a"))
    put(b, property_unit("from-b"))
    cfg = DictConfigSource({"kv_store.active": "a"})
    router = ActiveRouter(
        namespace="kv_store", instances={"a": a, "b": b}, config_source=cfg, default_name="a"
    )
    store = RoutingKVStore(router)
    assert ids(store, "s1") == {"from-a"}
    cfg.put("kv_store.active", "b")
    assert ids(store, "s1") == {"from-b"}
    store.clear_schema_source_index(SCOPE)
    assert store.get_schema_properties_by_source(SCOPE, "s1") is None
    assert ids(a, "s1") == {"from-a"}
    store.rebuild_schema_source_index(SCOPE)
    assert ids(store, "s1") == {"from-b"}


class ReadOnlySecurity(StorageSecurity):
    def __init__(self):
        self.calls = []

    def authorize(self, access, scope, action, resource):
        self.calls.append((scope, action))
        if action is not StorageAction.GET:
            raise PermissionDeniedError("only GET")


def test_lookup_authorized_as_get_and_maintenance_as_admin():
    raw = InMemoryKVStore(schema_source_index_enabled=True)
    put(raw, property_unit())
    security = ReadOnlySecurity()
    kv = CompositeStoreManager(kv=raw, security=security).kv()
    assert ids(kv, "s1") == {"p"}
    assert security.calls == [(SCOPE, StorageAction.GET)]
    with pytest.raises(PermissionDeniedError):
        kv.rebuild_schema_source_index(SCOPE)
    with pytest.raises(PermissionDeniedError):
        kv.clear_schema_source_index(SCOPE)
    assert security.calls[-1] == (SCOPE, StorageAction.ADMIN)


def test_encrypted_store_retains_decrypted_scan_without_plaintext_relations():
    from tests.unit.storage.test_encrypted_kv_store import _FakeSecurity

    raw = InMemoryKVStore()
    encrypted = EncryptedKVStore(raw, _FakeSecurity())
    unit = property_unit()
    put(encrypted, unit)
    assert encrypted.get_schema_properties_by_source(SCOPE, "s1") is None
    assert encrypted.scan(SCOPE) == [(memory_key(unit.id), dumps(unit))]
    assert raw.get(SCOPE, memory_key(unit.id)) != dumps(unit)
