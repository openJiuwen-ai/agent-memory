# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Use an explicitly dedicated empty Redis logical DB via SPACE_QUERY_TEST_REDIS_URL."""
from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
from jiuwen_memory.common.errors import ConflictError
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.space_impl.kv_space_manager import KVSpaceManager, _info_to_bytes
from jiuwen_memory.control.types import SpaceInfo, SpacePatch, SpaceSpec, SpaceStatus
from jiuwen_memory.storage._support import scope_segments
from jiuwen_memory.storage.kv_impl import redis_kv, redis_space_cache
from jiuwen_memory.storage.store_manager_impl.composite_store_manager import CompositeStoreManager
from tests.integration.storage.fixtures import ControlledRedisStore, ControlledSpaceCache
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.integration
ROOM = Scope(org="org", space="room")
USER = Scope(org="org", space="room", user="bob")
OPS = internal_context(ScopedAuthenticator(Scope(org="system", user="ops"), role=Role.ROOT))


def redis_key(scope, key):
    return ":".join((*scope_segments(scope), key))


@pytest.fixture
def store(request, monkeypatch):
    url = os.environ.get("SPACE_QUERY_TEST_REDIS_URL")
    if not url:
        pytest.skip("requires a dedicated empty Redis test DB")
    monkeypatch.setattr(redis_kv, "RedisSpaceCache", ControlledSpaceCache)
    options = getattr(request, "param", True)
    if isinstance(options, bool):
        options = {"space_query_index": options}
    kv = ControlledRedisStore(url=url, **{"space_query_batch_size": 3, **options})
    db = kv.client
    assert db.dbsize() == 0, "SPACE_QUERY_TEST_REDIS_URL must select an empty test DB"
    try:
        yield kv
    finally:
        kv.close()
        keys = list(db.scan_iter())
        if keys:
            db.unlink(*keys)
        db.close()


def metadata(space="room", org="org"):
    return _info_to_bytes(SpaceInfo(org=org, space=space, display_name=space))


@pytest.fixture
def api(store, monkeypatch):
    # Tests decide when the worker gets a turn; public API, auth and KV are real.
    if store.cache is not None:
        monkeypatch.setattr(store.cache, "_run", lambda: None)
    kernel = _build_kernel(kv=store, config={
        "permission": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
    })
    try:
        assert kernel.storage.kv().space_queries() is store.space_queries()
        yield kernel.api
    finally:
        kernel.close()
        if store.cache is not None:
            assert store.cache.stopped


def test_only_read_commands_and_existing_acl(store):
    db = store.client
    blob = metadata()
    db.set("org:room:_:_:_:/space/info", blob)
    db.set("org:room:bob:_:_:/messages/ttl", b"ok", ex=3600)
    deadline = db.pexpiretime("org:room:bob:_:_:/messages/ttl")
    foreign = {f"__jiuwen_space_v1__/{k}": b"foreign"
               for k in ("before", "permanent", "ttl0", "ttl1")}
    db.mset(foreign)
    parsed = urlsplit(os.environ["SPACE_QUERY_TEST_REDIS_URL"])
    username = "cache-test-" + secrets.token_hex(6)
    password = secrets.token_urlsafe(16)
    url = parsed._replace(netloc=f"{username}:{password}@{parsed.hostname}:{parsed.port}").geturl()
    reader = ControlledRedisStore(url=url)
    try:
        db.execute_command("ACL", "SETUSER", username, "reset", "on", ">" + password, "~*",
                           "+get", "+mget", "+scan", "+strlen", "+pttl", "+info", "+select")
        cache = reader.cache
        changes = db.info("persistence")["rdb_changes_since_last_save"]
        cache.refresh()
        assert cache.metadata("org") == [blob]
        assert cache.usage("org", "room") == (0, 1, len(blob) + 2)
        assert db.info("persistence")["rdb_changes_since_last_save"] == changes
        assert db.mget(list(foreign)) == list(foreign.values())
        assert not db.exists("_:_:_:_:_:/spaces/by-id/cm9vbQ")
        assert db.pexpiretime("org:room:bob:_:_:/messages/ttl") == deadline

        # Existing write ACL allows SET, denies Lua and accounting read commands.
        db.execute_command("ACL", "SETUSER", username, "+set", "-pttl")
        reader.insert(USER, "/memory/new", b"ok")
        assert cache.usage("org", "room") == (1, 1, len(blob) + 4)
        reader.update(USER, "/memory/new", b"updated")
        assert reader.get(USER, "/memory/new") == b"updated"
        assert cache.usage("org", "room") is None
    finally:
        reader.close()
        db.execute_command("ACL", "DELUSER", username)


def test_live_permanent_and_daily_expiring_usage(store):
    blob = metadata()
    store.insert(ROOM, "/space/info", blob)
    store.insert(USER, "/memory/empty", b"")
    store.insert(USER, "/messages/short", b"msg", ttl=1000)
    cache = store.cache
    cache.refresh()
    assert cache.usage("org", "room") == (1, 1, len(blob) + 3)
    store.update(USER, "/memory/empty", b"value")
    assert not store.compare_and_swap(USER, "/memory/empty", b"wrong", None)
    with pytest.raises(ConflictError):
        store.create_if_absent(USER, "/memory/empty", b"unexpected")
    assert cache.usage("org", "room") == (1, 1, len(blob) + 8)
    deadline = store.client.pexpiretime("org:room:bob:_:_:/messages/short")
    assert store.compare_exchange(USER, "/messages/short", b"msg", b"new")
    assert store.client.pexpiretime("org:room:bob:_:_:/messages/short") == deadline
    store.update(USER, "/memory/empty", b"temporary", ttl=1000)
    store.client.pexpireat("org:room:bob:_:_:/messages/short", 1)
    cache.refresh()
    assert cache.usage("org", "room") == (0, 1, len(blob) + 3)
    cache.expire_sample()
    cache.refresh()
    assert cache.usage("org", "room") == (1, 0, len(blob) + 9)
    assert store.compare_and_swap(USER, "/memory/empty", b"temporary", b"now permanent")
    cache.expire_sample()
    cache.refresh()
    assert cache.usage("org", "room") == (1, 0, len(blob) + 13)
    assert store.delete_if_value(USER, "/memory/empty", b"now permanent")
    assert cache.usage("org", "room") == (0, 0, len(blob))


@pytest.mark.parametrize("name", ["room", "team_ops"])
def test_concurrent_backfill_duplicates_and_deleted_keys(store, monkeypatch, name):
    room = Scope(org="org", space=name)
    user = Scope(org="org", space=name, user="bob")
    store.insert(room, "/space/info", metadata(name))
    for i in range(20):
        store.insert(user, f"/memory/{i}", b"old")
    cache = store.cache
    pages = cache.scan_pages
    scanning, resume = threading.Event(), threading.Event()

    def pause_scan(db, pattern, gen):
        for page in pages(db, pattern, gen):
            yield page
            if pattern.endswith(b":*:*:*:*:*"):
                if not scanning.is_set():
                    scanning.set()
                    assert resume.wait(5)
                yield page

    monkeypatch.setattr(cache, "_pages", pause_scan)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(cache.refresh)
        try:
            assert scanning.wait(5)
            assert cache.metadata("org") == [metadata(name)]
            assert cache.usage("org", name) is None
            for i in range(10):
                store.delete(user, f"/memory/{i}")
            store.update(user, "/memory/11", b"longer")
            store.create_if_absent(user, "/messages/x", b"x")
            store.replace_if_value(user, "/messages/x", b"x", b"xy")
            store.delete(user, "/messages/x")
            store.insert(user, "/messages/x", b"final")
        finally:
            resume.set()
        task.result(timeout=10)
    assert cache.usage("org", name) == (10, 1, len(metadata(name)) + 9 * 3 + 6 + 5)


def test_directory_order_recreation_and_legacy_alias(store, monkeypatch):
    for name in ("z", "a", "m"):
        store.insert(Scope(org="org", space=name), "/space/info", metadata(name))
    cache = store.cache
    cache.refresh()
    manager = KVSpaceManager(CompositeStoreManager(kv=store))
    try:
        with monkeypatch.context() as guard:
            def forbidden():
                pytest.fail("ready list enumerated KV scopes")
            guard.setattr(store, "scopes", forbidden)
            assert [v.space for v in manager.list("org", cursor="1", limit=1)] == ["m"]
            assert manager.list("other") == []
        manager.create(SpaceSpec(org="org", space="sales_ops"))
        with pytest.raises(ConflictError):
            manager.create(SpaceSpec(org="org", space="sales/ops"))
        assert manager.get("org", "sales_ops").space == "sales_ops"
        store.insert(Scope(org="org", space="m"), "/messages/temp", b"old", ttl=600)
        cache.expire_sample()
        cache.refresh()
        manager.delete("org", "m")
        manager.create(SpaceSpec(org="org", space="m"))
        cache.refresh()
        assert manager.usage("org", "m").storage_bytes == len(
            store.get(Scope(org="org", space="m"), "/space/info")
        )
    finally:
        manager.release_space_queries()


def test_daily_failure_expires_snapshot_and_retries(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    store.insert(USER, "/memory/ttl", b"v", ttl=600)
    cache = store.cache
    cache.refresh()
    store.delete(USER, "/memory/ttl")
    cache.expire_sample()
    with monkeypatch.context() as guard:
        def fail(*args):
            raise OSError("scan interrupted")
        guard.setattr(cache, "_pages", fail)
        with pytest.raises(OSError):
            cache.refresh()
    assert cache.usage("org", "room") is None
    cache.refresh()
    assert cache.usage("org", "room") == (0, 0, len(metadata()))


def test_cache_resets_when_source_changes(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    cache = store.cache
    cache.refresh()
    db = store.client
    monkeypatch.setattr(cache, "_source", lambda: (db, "different-run-id", 0))
    assert cache.usage("org", "room") is None
    assert cache.metadata("org") is None
    cache.refresh()
    assert cache.metadata("org") == [metadata()]


def test_eviction_keeps_directory_and_throttles_usage_rebuild(store, monkeypatch):
    now = 1000
    monkeypatch.setattr(redis_space_cache, "monotonic", lambda: now)
    db = store.client
    cache = store.cache
    evicted = 0
    monkeypatch.setattr(cache, "_source", lambda: (db, "same-run-id", evicted))
    store.insert(ROOM, "/space/info", metadata())
    store.insert(USER, "/messages/lost", b"old")
    cache.refresh()
    db.delete(redis_key(USER, "/messages/lost"))
    evicted += 1
    assert cache.metadata("org") == [metadata()]
    assert cache.usage("org", "room") is None
    assert store.space_queries().usage("org", "room") == (0, 0, len(metadata()))

    traversals = []
    pages = cache.scan_pages

    def under_pressure(client, pattern, generation):
        nonlocal evicted
        traversals.append(pattern)
        yield from pages(client, pattern, generation)
        evicted += 1

    with monkeypatch.context() as pressure:
        pressure.setattr(cache, "_pages", under_pressure)
        cache.refresh()
        for _ in range(10):
            evicted += 1
            assert cache.metadata("org") == [metadata()]
            cache.refresh()
        assert traversals == [b"*:*:*:*:*:*"]
    now += redis_space_cache.BACKFILL_INTERVAL
    cache.refresh()
    assert cache.usage("org", "room") == (0, 0, len(metadata()))


def test_live_read_after_next_eviction_does_not_join_an_older_scan(store, monkeypatch):
    db = store.client
    cache = store.cache
    evictions = 0
    monkeypatch.setattr(cache, "_source", lambda: (db, "same-run-id", evictions))
    store.insert(ROOM, "/space/info", metadata())
    store.insert(USER, "/messages/one", b"1")
    cache.refresh()
    evictions += 1
    entered, release = threading.Event(), threading.Event()
    reader = store.space_queries()
    scan = redis_space_cache.RedisSpaceReader(lambda: db, 3, None).usage

    def pause_after_read(org, space):
        row = scan(org, space)
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return row

    monkeypatch.setattr(reader, "_scan_usage", pause_after_read)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(reader.usage, "org", "room")
        try:
            assert entered.wait(5)
            db.delete(redis_key(USER, "/messages/one"))
            evictions += 1
            assert reader.usage("org", "room") == (0, 0, len(metadata()))
        finally:
            release.set()
        assert first.result(timeout=5) == (0, 1, len(metadata()) + 1)


def test_injected_allow_all_subclass_cannot_bypass_per_key_authorization(store):
    from jiuwen_memory.storage.security import AllowAllStorageSecurity

    class RestrictivePolicy(AllowAllStorageSecurity):
        def authorize(self, access, scope, action, resource):
            raise PermissionError("per-key policy")

    protected = CompositeStoreManager(kv=store, security=RestrictivePolicy()).kv()
    assert protected.space_queries() is None
    with pytest.raises(PermissionError, match="per-key policy"):
        protected.get(ROOM, "/space/info")


def test_directory_build_preserves_interleaved_create_delete(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    other = Scope(org="org", space="new")
    store.insert(other, "/memory/preregistered", b"earlier")
    cache = store.cache
    pages = cache.scan_pages

    def interleave(db, pattern, gen):
        if pattern.endswith(b"/space/info"):
            old = list(pages(db, pattern, gen))
            store.delete(ROOM, "/space/info")
            store.insert(other, "/space/info", metadata("new"))
            yield from old
        else:
            yield from pages(db, pattern, gen)

    monkeypatch.setattr(cache, "_pages", interleave)
    cache.refresh()
    assert cache.metadata("org") == [metadata("new")]
    assert cache.usage("org", "new") == (1, 0, len(metadata("new")) + 7)
    assert cache.usage("org", "room") is None


def test_committed_write_with_observer_failure_recovers(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    cache = store.cache
    cache.refresh()
    original = store.client.pipeline
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("unavailable after the conditional write")
        return original(*args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(store.client, "pipeline", interrupted)
        assert store.compare_and_swap(USER, "/messages/new", None, b"abc")
    assert store.get(USER, "/messages/new") == b"abc"
    assert cache.usage("org", "room") is None
    cache.refresh()
    assert cache.usage("org", "room") == (0, 1, len(metadata()) + 3)


def test_independent_writes_and_idle_poll_do_not_wait_for_32_slow_keys(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    cache = store.cache
    cache.refresh()
    ready = threading.Barrier(33)
    release = threading.Event()
    write = store.client.set

    def slow_writes(name, *args, **kwargs):
        if "/memory/slow-" in name:
            ready.wait(timeout=10)
            assert release.wait(10)
        return write(name, *args, **kwargs)

    monkeypatch.setattr(store.client, "set", slow_writes)
    with ThreadPoolExecutor(max_workers=34) as pool:
        waiting = [pool.submit(store.insert, USER, f"/memory/slow-{i}", b"s") for i in range(32)]
        try:
            ready.wait(timeout=10)
            pool.submit(cache.refresh).result(timeout=2)
            pool.submit(store.insert, USER, "/memory/independent", b"f").result(timeout=2)
            assert store.get(USER, "/memory/independent") == b"f"
        finally:
            release.set()
        for writer in waiting:
            writer.result(timeout=5)
    assert cache.usage("org", "room") == (33, 0, len(metadata()) + 33)


def test_same_key_writes_and_publish_remain_ordered(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    store.insert(USER, "/memory/same", b"x")
    cache = store.cache
    cache.refresh()
    cache.expire_sample()
    entered, release = threading.Event(), threading.Event()
    write = store.client.set

    def pause_first(key, value, **kwargs):
        if value == b"first":
            entered.set()
            assert release.wait(10)
        return write(key, value, **kwargs)

    monkeypatch.setattr(store.client, "set", pause_first)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(store.update, USER, "/memory/same", b"first")
        try:
            assert entered.wait(5)
            second = pool.submit(store.update, USER, "/memory/same", b"second")
            refresh = pool.submit(cache.refresh)
        finally:
            release.set()
        for task in (first, second, refresh):
            task.result(timeout=5)
    assert store.get(USER, "/memory/same") == b"second"
    assert cache.usage("org", "room") == (1, 0, len(metadata()) + 6)


def test_hot_plain_writes_only_sample_unknown_images(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    cache = store.cache
    cache.refresh()
    pipelines = []
    original = store.client.pipeline

    def count_samples(*args, **kwargs):
        pipelines.append(1)
        return original(*args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(store.client, "pipeline", count_samples)
        store.create_if_absent(USER, "/messages/a", b"x")
        store.insert(USER, "/messages/b", b"", ttl=60)
        assert len(pipelines) == 0
        store.update(USER, "/messages/b", b"now")
        assert len(pipelines) == 1
        store.delete(USER, "/messages/a")
        assert len(pipelines) == 2
        assert store.compare_exchange(USER, "/messages/b", b"now", b"done")
        assert len(pipelines) == 4
    assert cache.usage("org", "room") == (0, 1, len(metadata()) + 4)


@pytest.mark.parametrize("store", [{"space_query_batch_size": 100}], indirect=True)
def test_registrations_share_one_backfill_and_pending_usage_is_live(store, monkeypatch, request):
    now = 1000
    monkeypatch.setattr(redis_space_cache, "monotonic", lambda: now)
    cache = store.cache
    monkeypatch.setattr(cache, "_run", lambda: None)
    store.client.mset({f"unrelated-{i}": b"x" for i in range(4000)})
    store.insert(ROOM, "/space/info", metadata())
    manager = KVSpaceManager(CompositeStoreManager(kv=store))
    scans = []
    pages = cache.scan_pages

    def count_passes(db, pattern, gen):
        scans.append(pattern)
        yield from pages(db, pattern, gen)

    def scan_calls():
        return store.client.info("commandstats").get("cmdstat_scan", {}).get("calls", 0)

    try:
        cache.refresh()
        monkeypatch.setattr(cache, "_pages", count_passes)
        first_registration = now
        for name in ("a", "b", "c"):
            scope = Scope(org="org", space=name)
            store.insert(scope, "/memory/old", b"before")
            store.insert(scope, "/space/info", metadata(name))
            before = scan_calls()
            cache.refresh()
            assert scan_calls() == before
            assert cache.usage("org", name) is None
            assert manager.usage("org", name).storage_bytes == len(metadata(name)) + 6
            store.update(scope, "/memory/old", b"after")
            assert manager.usage("org", name).storage_bytes == len(metadata(name)) + 5
            assert cache.usage("org", "room") == (0, 0, len(metadata()))
            now += 60
        assert scans == []
        now = first_registration + 5 * 60
        before = scan_calls()
        cache.refresh()
        cursor_calls = scan_calls() - before
        assert cursor_calls > 0
        request.node.user_properties.append(("batched_redis_scan_calls", cursor_calls))
        assert len(scans) == 1, "all pending groups must share one full cursor traversal"
        for name in ("a", "b", "c"):
            assert cache.usage("org", name) == (1, 0, len(metadata(name)) + 5)
        before = scan_calls()
        cache.refresh()
        assert scan_calls() == before
    finally:
        manager.release_space_queries()


def test_list_spaces_realtime_through_restart_and_underscore_name(api, store):
    cache = store.cache
    store.insert(Scope(org="org"), "/unregistered", b"x")

    def listed(status=None):
        spaces = api.list_spaces("org", status=status, security=OPS)
        return [(info.space, info.display_name) for info in spaces]

    assert listed() == []
    api.create_space(SpaceSpec(org="org", space="_", display_name="first"), security=OPS)
    assert listed() == [("_", "first")]  # cold fallback
    child = Scope(org="org", space="_", user="bob")
    payload = dumps(MemoryUnit(id="child", scope=child))
    store.insert(child, "/memory/child", payload)
    initial = api.space_usage("org", "_", security=OPS)
    assert initial.memory_count == 1
    cache.refresh()
    assert listed() == [("_", "first")]
    assert api.space_usage("org", "_", security=OPS) == initial
    store.delete(child, "/memory/child")
    after_delete = api.space_usage("org", "_", security=OPS)
    assert after_delete.storage_bytes == initial.storage_bytes - len(payload)
    api.create_space(SpaceSpec(org="org", space="other"), security=OPS)
    assert listed() == [("_", "first"), ("other", "other")]
    api.update_space("org", "_", SpacePatch(display_name="renamed"), security=OPS)
    assert listed() == [("_", "renamed"), ("other", "other")]
    api.archive_space("org", "_", security=OPS)
    assert listed(SpaceStatus.ARCHIVED) == [("_", "renamed")]
    assert listed(SpaceStatus.ACTIVE) == [("other", "other")]

    # Restart from only the old-format business keys in the same Redis database.
    url = os.environ["SPACE_QUERY_TEST_REDIS_URL"]
    reopened = ControlledRedisStore(url=url)
    reopened_cache = reopened.cache
    manager = KVSpaceManager(CompositeStoreManager(kv=reopened))
    try:
        reopened_cache.refresh()
        assert [info.space for info in manager.list("org")] == ["_", "other"]
        assert manager.get("org", "_").status is SpaceStatus.ARCHIVED
    finally:
        manager.release_space_queries()
        reopened.close()

    api.delete_space("org", "_", security=OPS)
    assert listed() == [("other", "other")]
    api.create_space(SpaceSpec(org="org", space="_", display_name="new"), security=OPS)
    assert listed() == [("_", "new"), ("other", "other")]
    transient = Scope(org="org", space="transient")
    store.insert(transient, "/space/info", metadata("transient"), ttl=3600)
    assert listed()[-1] == ("transient", "transient")
    store.client.pexpireat(redis_key(transient, "/space/info"), 1)
    assert listed() == [("_", "new"), ("other", "other")]


def test_space_usage_realtime_permanent_and_24h_ttl_deadline(api, store, monkeypatch):
    now = 1000
    monkeypatch.setattr(redis_space_cache, "monotonic", lambda: now)
    cache = store.cache
    api.create_space(SpaceSpec(org="org", space="room"), security=OPS)
    base = len(store.get(ROOM, "/space/info"))

    def usage():
        row = api.space_usage("org", "room", security=OPS)
        return row.memory_count, row.message_count, row.storage_bytes - base

    store.insert(USER, "/memory/old", b"old")
    store.insert(USER, "/messages/expires", b"snapshot", ttl=3600)
    assert usage() == (1, 1, 11)
    first_sample = now
    pages = cache.scan_pages

    def slow_build(db, pattern, gen):
        nonlocal now
        yield from pages(db, pattern, gen)
        if not pattern.endswith(b"/space/info"):
            now += 60

    with monkeypatch.context() as guard:
        guard.setattr(cache, "_pages", slow_build)
        cache.refresh()
    assert usage() == (1, 1, 11)
    store.insert(USER, "/messages/permanent", b"")
    assert usage() == (1, 2, 11)
    store.update(USER, "/memory/old", b"longer")
    assert usage() == (1, 2, 14)
    assert not store.compare_and_swap(USER, "/memory/old", b"wrong", None)
    assert usage() == (1, 2, 14)
    assert store.compare_and_swap(USER, "/memory/old", b"longer", b"x")
    assert usage() == (1, 2, 9)
    store.delete(USER, "/memory/old")
    assert usage() == (0, 2, 8)
    store.insert(USER, "/memory/transition", b"temp", ttl=3600)
    store.update(USER, "/memory/transition", b"live")
    assert usage() == (1, 2, 12)
    store.update(USER, "/memory/transition", b"temp", ttl=3600)
    assert usage() == (0, 2, 8)
    store.delete(USER, "/memory/transition")

    # Force actual Redis expiry, then advance only the engine's sampling clock.
    store.client.pexpireat(redis_key(USER, "/messages/expires"), 1)
    assert not store.exists(USER, "/messages/expires")
    store.insert(USER, "/memory/new-ttl", b"tt", ttl=3600)
    now = first_sample + 24 * 3600 - 1
    cache.refresh()
    store.update(USER, "/messages/permanent", b"abc")
    assert usage() == (0, 2, 11)
    now += 1
    assert usage() == (1, 1, 5)  # 24h boundary before the next worker poll
    with monkeypatch.context() as guard:
        def fail_scan(*args):
            raise OSError("sampling unavailable")
        guard.setattr(cache, "_pages", fail_scan)
        with pytest.raises(OSError):
            cache.refresh()
        store.delete(USER, "/messages/permanent")
        assert usage() == (1, 0, 2)
    cache.refresh()
    assert cache.usage("org", "room") == (1, 0, base + 2)
    assert usage() == (1, 0, 2)


def test_literal_redis_patterns_in_usage(api, store):
    cache = store.cache
    for name in ("team?", "team1", "team*", "team[1]", r"team\1"):
        api.create_space(SpaceSpec(org="org", space=name), security=OPS)
        scope = Scope(org="org", space=name, user="bob")
        store.insert(scope, "/messages/own", name.encode())
    store.insert(Scope(org="org", space="team1", user="bob"), "/messages/extra", b"x")

    def usage(name):
        row = api.space_usage("org", name, security=OPS)
        return row.message_count, row.storage_bytes

    def rows():
        return {name: usage(name) for name in ("team?", "team1", "team*", "team[1]", r"team\1")}

    expected = {name: (1 + int(name == "team1"), len(name.encode()) + int(name == "team1")
                       + len(store.get(Scope(org="org", space=name), "/space/info")))
                for name in ("team?", "team1", "team*", "team[1]", r"team\1")}
    assert rows() == expected
    cache.refresh()
    assert rows() == expected
    cache.expire_sample()
    assert rows() == expected
    cache.refresh()
    assert rows() == expected


@pytest.mark.parametrize("name", ["_", "/", ":", "dept/sales", "dept:sales"])
def test_original_names_across_cold_warm_and_disabled_cache(store, monkeypatch, name):
    org = "tenant/one"
    scope = Scope(org=org, space=name)
    # Old-format metadata; there need not be a global name registry record.
    store.insert(scope, "/space/info", metadata(name, org))
    for enabled in (True, False):
        kv = ControlledRedisStore(
            url=os.environ["SPACE_QUERY_TEST_REDIS_URL"], space_query_index=enabled,
        )
        cache = kv.cache
        if cache is not None:
            monkeypatch.setattr(cache, "_run", lambda: None)
        manager = KVSpaceManager(CompositeStoreManager(kv=kv))
        try:
            assert manager.get(org, name).space == name
            assert [s.space for s in manager.list(org)] == [name]
            assert manager.list("tenant_one") == []
            assert manager.usage("tenant_one", name).storage_bytes == 0
            if cache is not None:
                cache.refresh()
                assert [s.space for s in manager.list(org)] == [name]
                assert manager.list("tenant_one") == []
                assert manager.usage("tenant_one", name).storage_bytes == 0
        finally:
            manager.release_space_queries()
            kv.close()


def test_expired_concurrent_usage_shares_one_scan_and_fences_writes(store, monkeypatch, request):
    cache = store.cache
    monkeypatch.setattr(cache, "_run", lambda: None)
    store.client.mset({f"unrelated-{i}": b"x" for i in range(4000)})
    store.insert(ROOM, "/space/info", metadata())
    store.insert(USER, "/messages/old", b"x")
    manager = KVSpaceManager(CompositeStoreManager(kv=store))
    scanning, resume, joined = threading.Event(), threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    followers = 0

    class ObservedFuture(redis_space_cache.Future):
        def result(self, timeout=None):
            nonlocal followers
            with counter_lock:
                followers += 1
                if followers == 5:
                    joined.set()
            return super().result(timeout)

    def scan_calls():
        return store.client.info("commandstats")["cmdstat_scan"]["calls"]

    reader = store.space_queries()
    scan = redis_space_cache.RedisSpaceReader(lambda: store.client, 3, None).usage

    def pause_first(org, space):
        value = scan(org, space)
        if not scanning.is_set():
            scanning.set()
            assert resume.wait(10)
        return value

    try:
        cache.refresh()
        cache.expire_sample()
        before = scan_calls()
        baseline = manager.usage("org", "room")
        one_scan = scan_calls() - before
        assert one_scan > 0
        monkeypatch.setattr(redis_space_cache, "Future", ObservedFuture)
        monkeypatch.setattr(reader, "_scan_usage", pause_first)
        with ThreadPoolExecutor(max_workers=6) as pool:
            before = scan_calls()
            readers = [pool.submit(manager.usage, "org", "room") for _ in range(6)]
            try:
                assert scanning.wait(5) and joined.wait(5)
                six_scans = scan_calls() - before
                assert six_scans == one_scan
                request.node.user_properties.extend([("single_scan_calls", one_scan),
                                                      ("six_reader_scan_calls", six_scans)])
                store.insert(USER, "/messages/new", b"changed")
                after_write = manager.usage("org", "room")
                assert after_write.message_count == 2
                assert after_write.storage_bytes == baseline.storage_bytes + 7
            finally:
                resume.set()
            assert all(reader.result(timeout=5) == baseline for reader in readers)
        with monkeypatch.context() as guard:
            def fail(org, space):
                raise OSError("read interrupted")
            guard.setattr(reader, "_scan_usage", fail)
            with pytest.raises(OSError, match="interrupted"):
                manager.usage("org", "room")
        assert manager.usage("org", "room") == after_write
    finally:
        manager.release_space_queries()


@pytest.mark.parametrize("store", [True, False], indirect=True)
@pytest.mark.parametrize("name", ["_", "/"])
def test_ambiguous_bucket_counts_only_proven_records(api, store, caplog, name):
    org_level = Scope(org="org")
    room = Scope(org="org", space=name)
    user = Scope(org="org", space=name, user="u")
    org_user = Scope(org="org", user="u")
    cache = store.cache
    api.create_space(SpaceSpec(org="org", space=name), security=OPS)
    base = len(store.get(room, "/space/info"))
    owned = dumps(MemoryUnit(id="owned", scope=user))
    foreign = dumps(MemoryUnit(id="foreign", scope=org_user))
    store.insert(org_level, "/config", b"short")
    store.insert(org_user, "/messages/foreign", foreign)
    store.insert(user, "/messages/opaque", b"old opaque message")
    store.insert(user, "/messages/owned", owned)
    store.insert(room, "/memory/invalid", b"\xff")
    store.insert(org_level, "/control", b'{"org":"org","space":"_"}')

    def expected():
        usage = api.space_usage("org", name, security=OPS)
        assert (usage.memory_count, usage.message_count, usage.storage_bytes) == (
            0, 1, base + len(owned)
        )

    expected()
    assert "without verified space ownership" in caplog.text
    if cache is not None:
        cache.refresh()
        assert cache.usage("org", name) == (0, 1, base + len(owned))
    store.update(org_level, "/config", b"short+extra")
    expected()
    alternate = dumps(MemoryUnit(id="owned", scope=org_user))
    store.update(org_user, "/messages/owned", alternate)
    assert api.space_usage("org", name, security=OPS).message_count == 0
    store.update(user, "/messages/owned", owned)
    expected()
    if cache is not None:
        cache.expire_sample()
        expected()
        cache.refresh()
        expected()


def test_shared_prefix_ttl_and_read_only_refresh(store):
    room = Scope(org="org", space="_")
    record = dumps(MemoryUnit(id="ttl", scope=room))
    store.insert(room, "/space/info", metadata("_"))
    store.insert(room, "/messages/ttl", record, ttl=600)
    store.insert(Scope(org="org"), "/messages/opaque", b"opaque", ttl=600)
    cache = store.cache
    db = store.client
    before = db.info("persistence")["rdb_changes_since_last_save"]
    cache.refresh()
    assert db.info("persistence")["rdb_changes_since_last_save"] == before
    assert cache.usage("org", "_") == (0, 1, len(metadata("_")) + len(record))
    db.pexpireat(redis_key(room, "/messages/ttl"), 1)
    cache.expire_sample()
    cache.refresh()
    assert cache.usage("org", "_") == (0, 0, len(metadata("_")))


@pytest.mark.parametrize("store", [True, False], indirect=True)
@pytest.mark.parametrize(("org", "name"), [("acme", "team_ops"), ("org_ops", "team-ops")])
def test_literal_underscore_keeps_legacy_trajectory_usage(api, store, caplog, org, name):
    from datetime import UTC, datetime, timedelta

    from jiuwen_memory.common.type_def import trajectory_key
    from jiuwen_memory.construction.experience import ExecutionTrajectoryRecord, TrajectoryStore

    room = Scope(org=org, space=name)
    user = Scope(org=org, space=name, user="u")
    api.create_space(SpaceSpec(org=org, space=name), security=OPS)
    record = ExecutionTrajectoryRecord(
        trajectory_id="r1", expires_at=(datetime.now(UTC) + timedelta(days=10)).isoformat(),
        messages=[{"role": "user", "content": "x"}],
    )
    TrajectoryStore(store).save(user, "digest", record)
    key = trajectory_key("digest", "r1")
    expected = len(store.get(room, "/space/info")) + len(store.get(user, key))
    db = store.client
    deadline = db.pexpiretime(redis_key(user, key))

    def usage():
        row = api.space_usage(org, name, security=OPS)
        return row.memory_count, row.message_count, row.storage_bytes

    changes = db.info("persistence")["rdb_changes_since_last_save"]
    assert usage() == (0, 0, expected)
    cache = store.cache
    if cache is not None:
        cache.refresh()
        assert cache.usage(org, name) == usage() == (0, 0, expected)
        cache.expire_sample()
        assert usage() == (0, 0, expected)
        cache.refresh()
        assert cache.usage(org, name) == (0, 0, expected)
    assert deadline > 0 and db.pexpiretime(redis_key(user, key)) == deadline
    assert db.info("persistence")["rdb_changes_since_last_save"] == changes

    store.insert(user, "/messages/legacy", b"old")
    assert usage() == (0, 1, expected + 3)
    store.update(user, "/messages/legacy", b"longer")
    assert usage() == (0, 1, expected + 6)
    store.delete(user, "/messages/legacy")
    assert usage() == (0, 0, expected)
    assert "without verified space ownership" not in caplog.text


@pytest.mark.parametrize("store", [True, False], indirect=True)
def test_nonempty_alias_only_counts_verified_records_and_preserves_delete_scope(api, store, caplog):
    historical = Scope(org="org", space="dept_sales", user="u")
    real = Scope(org="org", space="dept/sales", user="u")
    other = dumps(MemoryUnit(id="other", scope=historical))
    own = dumps(MemoryUnit(id="own", scope=real))
    store.insert(historical, "/messages/other", other)
    api.create_space(SpaceSpec(org="org", space=real.space), security=OPS)
    store.insert(real, "/messages/own", own)
    store.insert(real, "/messages/undocumented", b"old")
    info_bytes = store.get(Scope(org="org", space=real.space), "/space/info")

    def check(payload):
        row = api.space_usage("org", real.space, security=OPS)
        assert (row.message_count, row.storage_bytes) == (1, len(info_bytes) + len(payload))

    check(own)
    assert "without verified space ownership" in caplog.text
    cache = store.cache
    if cache is not None:
        cache.refresh()
        check(own)
        longer = own + b" "
        store.update(real, "/messages/own", longer)
        check(longer)
        store.update(historical, "/messages/other", other + b" ")
        check(longer)
        cache.expire_sample()
        check(longer)

    # Baseline public deletion selects logical scopes. The usage scan must never
    # widen its deletion set to include this historical sibling's user records.
    api.delete_space("org", real.space, security=OPS)
    assert store.exists(historical, "/messages/other")
    assert api.list_spaces("org", security=OPS) == []


def test_encrypted_list_uses_original_kv_view(store):
    from jiuwen_memory.common.security.cryptography.cryptography_impl.local_envelope import (
        LocalEnvelopeCryptographyProvider,
        LocalKeyProvider,
    )
    from jiuwen_memory.storage.kv_impl.encrypted_kv_store import EncryptedKVStore

    encrypted = EncryptedKVStore(store, LocalEnvelopeCryptographyProvider(
        LocalKeyProvider(key_hex=secrets.token_hex(32))
    ))
    manager = KVSpaceManager(CompositeStoreManager(kv=encrypted))
    try:
        for name in ("ordinary", "_"):
            manager.create(SpaceSpec(org="org", space=name))
        assert encrypted.space_queries() is None
        assert [v.space for v in manager.list("org")] == ["ordinary"]
        assert manager.get("org", "_").space == "_"
    finally:
        manager.release_space_queries()


@pytest.mark.parametrize("store", [True, False], indirect=True)
def test_business_write_commands_and_results_with_and_without_cache(store, monkeypatch):
    from jiuwen_memory.common.errors import NotFoundError

    store.insert(ROOM, "/space/info", metadata())
    if store.cache is not None:
        store.cache.refresh()
    db = store.client
    commands = []
    execute = db.execute_command

    def record(command, *args, **kwargs):
        commands.append((command, args))
        return execute(command, *args, **kwargs)

    monkeypatch.setattr(db, "execute_command", record)
    key = "/business"
    assert store.insert(USER, key, b"one", ttl=60) is None
    with pytest.raises(ConflictError):
        store.insert(USER, key, b"two")
    assert store.update(USER, key, b"three") is None
    assert store.compare_and_swap(USER, key, b"two", None) is False
    assert store.compare_and_swap(USER, key, b"three", b"four") is True
    assert store.delete_if_value(USER, key, b"wrong") is False
    assert store.delete(USER, key) is None
    assert store.delete(USER, key) is None
    with pytest.raises(NotFoundError):
        store.update(USER, key, b"missing")
    assert [cmd.upper() for cmd, _ in commands] == [
        "SET", "SET", "SET", "EVAL", "EVAL", "EVAL", "DEL", "DEL", "SET"
    ]
    name = redis_key(USER, key)
    assert commands[0][1][:2] == (name, b"one")
    assert set(commands[0][1][2:]) == {"NX", "PX", 60_000}
    assert commands[2][1] == (name, b"three", "XX")


def test_first_build_overlapping_unobserved_write_reloads_baseline(store, monkeypatch):
    store.insert(ROOM, "/space/info", metadata())
    entered, resume = threading.Event(), threading.Event()
    db = store.client
    original = db.set

    def pause_before_set(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set", pause_before_set)
    cache = store.cache
    reader = store.space_queries()
    with ThreadPoolExecutor() as pool:
        writer = pool.submit(store.insert, USER, "/messages/first", b"x")
        try:
            assert entered.wait(5)
            cache.refresh()
            assert reader.usage("org", "room") == (0, 0, len(metadata()))
        finally:
            resume.set()
        assert writer.result(timeout=5) is None
    assert reader.usage("org", "room") == (0, 1, len(metadata()) + 1)
    cache.refresh()
    assert cache.usage("org", "room") == reader.usage("org", "room")


def test_failed_kernel_assembly_releases_its_cache_reader(store, monkeypatch):
    from jiuwen_memory.api.memory_api_impl import assembly

    cache = store.cache
    monkeypatch.setattr(cache, "_run", cache.wait_for_shutdown)

    def fail(*args, **kwargs):
        assert cache.users == 1
        raise RuntimeError("scheduler assembly failed")

    monkeypatch.setattr(assembly.SchedulerProducer, "dep", fail)
    with pytest.raises(RuntimeError, match="scheduler assembly failed"):
        _build_kernel(kv=store)
    assert cache.stopped
    store.health()
