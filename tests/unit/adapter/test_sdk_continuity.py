"""SDK continuity resource lifetime and real Provider-to-MemoryAPI integration."""

import asyncio
import threading
from dataclasses import asdict
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from jiuwen_memory.api import (
    ConflictError,
    RateLimitedError,
    Scope,
    SpaceSpec,
    ValidationError,
)
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.security.types import Role
from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import (
    AgentMemoryMemoryProvider,
    _HttpClient,
)
from jiuwen_memory_adapter.jiuwenswarm.sdk_runtime import (
    OperationWaitTimeout,
    _SharedSDKRuntimePool,
)
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def config_for(tmp_path):
    config = yaml.safe_load(Path("examples/role_memory/server.yaml").read_text(encoding="utf-8"))
    for name, spec in config["kv_store"].items():
        spec["params"]["db_path"] = str(tmp_path / f"{name}.db")
    config["permission"]["default"]["params"]["db_path"] = str(tmp_path / "permissions.db")
    config["continuity"]["default"]["params"] = {
        "mode": "continuity", "single_writer": True, "registry_store": "registry",
    }
    config["globals"]["continuity_connectors"] = {
        "host": {"actor": asdict(Scope(org="demo", user="owner")),
                 "projects": ["role-project"], "enabled": True},
    }
    return config


class Runtime:
    def __init__(self, api=None):
        self.api = api
        self.closed = False
        self.close_started = threading.Event()
        self.allow_close = threading.Event()
        self.allow_close.set()

    def start(self):
        pass

    def close(self, *, wait=True):
        self.close_started.set()
        assert self.allow_close.wait(5)
        self.closed = True


def test_pool_timeout_tracks_actual_write_and_release(tmp_path):
    async def run():
        pool, runtime = _SharedSDKRuntimePool(), Runtime()
        config = config_for(tmp_path)
        a = pool.acquire(config, "owner", lambda: runtime)
        b = pool.acquire(config, "owner", lambda: pytest.fail("second runtime created"))
        entered, finish = threading.Event(), threading.Event()

        def operation():
            entered.set()
            assert finish.wait(5)
            assert not runtime.closed
            return "committed"

        with pytest.raises(OperationWaitTimeout):
            await pool.submit(a, operation, timeout=0.01)
        assert entered.is_set()
        pool.release(a)
        release = asyncio.create_task(asyncio.to_thread(pool.release, b))
        await asyncio.sleep(0.01)
        assert not runtime.closed and not release.done()
        finish.set()
        await release
        assert runtime.closed
    asyncio.run(run())


def test_pool_waits_for_close_and_rejects_configuration_conflict(tmp_path):
    pool, first = _SharedSDKRuntimePool(), Runtime()
    config = config_for(tmp_path)
    lease = pool.acquire(config, "owner", lambda: first)
    with pytest.raises(ConflictError):
        pool.acquire(config, "other", Runtime)
    changed = config_for(tmp_path)
    changed["globals"]["embedder_dim"] = 100
    with pytest.raises(ConflictError):
        pool.acquire(changed, "owner", Runtime)
    first.allow_close.clear()
    closing = threading.Thread(target=pool.release, args=(lease,))
    closing.start()
    assert first.close_started.wait(3)
    obtained = []
    acquiring = threading.Thread(
        target=lambda: obtained.append(pool.acquire(config, "owner", Runtime)),
    )
    acquiring.start()
    assert not obtained
    first.allow_close.set()
    closing.join(5)
    acquiring.join(5)
    assert not closing.is_alive() and not acquiring.is_alive()
    assert obtained[0].entry.runtime is not first
    pool.release(obtained[0])


@pytest.mark.parametrize("task_embedding", [True, False])
def test_two_providers_resume_after_shutdown_and_restart(tmp_path, caplog, task_embedding):
    provider_logger = "jiuwenswarm.agents.harness.common.memory.external.agent_memory_provider"
    caplog.set_level("INFO", logger="jiuwen_memory_adapter")
    caplog.set_level("INFO", logger=provider_logger)

    async def run():
        config = config_for(tmp_path)
        if not task_embedding:
            config["continuity"]["default"]["params"]["embedder"] = None
        owner = Scope(org="demo", user="owner")

        def security(role=Role.USER):
            return internal_context(ScopedAuthenticator(owner, role=role))

        def provider():
            return AgentMemoryMemoryProvider(
                config_dict=config, continuity_enabled=True, continuity_mode="continuity",
                security_provider=security, portal={"enabled": False},
            )

        a, b = provider(), provider()
        scope = {"org": "demo", "space": "role-project"}
        try:
            await asyncio.gather(a.initialize(session_id="A"), b.initialize(session_id="B"))
            assert a.runtime is b.runtime
            a.runtime.api.create_space(SpaceSpec(org="demo", space="role-project", owner=owner),
                                       security=security(Role.ADMIN))
            task = await a.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "create", "request_id": "create", "title": "memory report"}})
            bound = await a.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "bind", "request_id": "bind-a", "task_id": task["task_id"],
                "expected_revision": task["revision"], "connector_id": "host", "context_id": "A"}})
            receipt = await a.sync_turn(
                "research memory", "completed first section", scope=scope,
                task_id=task["task_id"], binding_id=bound["task"]["bindings"][0]["binding_id"],
                event_id="turn-1",
            )
            assert receipt["units"]
            await a.shutdown()
            found = await b.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "find", "connector_id": "host", "context_id": "B", "query": "memory"}})
            assert found["tasks"][0]["task_id"] == task["task_id"]
            assert found["tasks"][0]["match_type"] == (
                "description_semantic" if task_embedding else "description_bm25"
            )
            view = await b.prefetch("", scope=scope, task_id=task["task_id"], top_k=1)
            assert "completed first section" in view
            with pytest.raises(ValidationError, match="authentication"):
                await b.continuity_request("continuity_task", {"scope": scope, "security": {},
                                            "request": {"operation": "find"}})
        finally:
            await a.shutdown()
            await b.shutdown()
        c = provider()
        try:
            await c.initialize(session_id="C")
            restored = await c.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "get", "task_id": task["task_id"]}})
            assert restored["title"] == "memory report"
            candidates = await c.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "find", "query": "memory"}})
            assert candidates["tasks"][0]["task_id"] == task["task_id"]
        finally:
            await c.shutdown()
    asyncio.run(run())
    messages = []
    for record in caplog.records:
        if record.name.startswith("jiuwen_memory_adapter"):
            messages.append(record.getMessage())
        elif record.name == provider_logger:
            messages.append(record.getMessage())
    assert any("[continuity.request.success]" in message and "operation=bind" in message
               and "binding_ids=[" in message for message in messages)
    assert any(
        "[continuity.prefetch.result]" in message and "blocking_gaps=" in message
        for message in messages
    )
    assert any("[continuity.runtime.reuse]" in message for message in messages)
    assert not any("completed first section" in message for message in messages)


def test_pool_cancellation_does_not_cancel_operation(tmp_path):
    async def run():
        pool, runtime = _SharedSDKRuntimePool(), Runtime()
        lease = pool.acquire(config_for(tmp_path), "owner", lambda: runtime)
        entered, finish = threading.Event(), threading.Event()

        def operation():
            entered.set()
            assert finish.wait(5)
            assert not runtime.closed
            return "committed"

        pending = asyncio.create_task(pool.submit(lease, operation))
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release = asyncio.create_task(asyncio.to_thread(pool.release, lease))
        finish.set()
        await release
        assert runtime.closed
    asyncio.run(run())


def test_pool_startup_rollback_and_close_failure_are_observable(tmp_path):
    class BadStart(Runtime):
        def start(self):
            raise ValueError("startup failed")

    pool, broken = _SharedSDKRuntimePool(), BadStart()
    config = config_for(tmp_path)
    with pytest.raises(ValueError, match="startup failed"):
        pool.acquire(config, "owner", lambda: broken)
    assert broken.closed

    class BadClose(Runtime):
        def close(self, *, wait=True):
            raise RuntimeError("close failed")

    lease = pool.acquire(config, "owner", BadClose)
    with pytest.raises(RuntimeError, match="close failed"):
        pool.release(lease)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        pool.acquire(config, "owner", Runtime)


def test_pool_startup_and_cleanup_errors_preserve_explicit_cause(tmp_path):
    class BadStartupAndCleanup(Runtime):
        def start(self):
            raise ValueError("startup failed")

        def close(self, *, wait=True):
            raise RuntimeError("cleanup failed")

    pool = _SharedSDKRuntimePool()
    config = config_for(tmp_path)
    with pytest.raises(ExceptionGroup, match="startup and cleanup failed") as error:
        pool.acquire(config, "owner", BadStartupAndCleanup)
    startup, cleanup = error.value.exceptions
    assert isinstance(startup, ValueError)
    assert isinstance(cleanup, RuntimeError)
    assert error.value.__cause__ is startup
    with pytest.raises(RuntimeError, match="previous SDK runtime cleanup failed") as failed:
        pool.acquire(config, "owner", Runtime)
    assert failed.value.__cause__ is cleanup


def test_pool_distinguishes_operation_timeout_from_wait_timeout(tmp_path):
    async def run():
        pool = _SharedSDKRuntimePool()
        lease = pool.acquire(config_for(tmp_path), "owner", Runtime)

        def operation():
            raise TimeoutError("database operation timed out")

        try:
            with pytest.raises(TimeoutError, match="database operation timed out") as error:
                await pool.submit(lease, operation)
            assert not isinstance(error.value, OperationWaitTimeout)
        finally:
            pool.release(lease)
    asyncio.run(run())


def test_http_revision_conflict_is_not_an_unknown_write(monkeypatch):
    async def run():
        responses = [
            {"error": "ConflictError", "message": "stale control revision"},
            {"error": "WriteIncompleteError", "status": "partial", "operation_id": "op",
             "refs": [], "retry_action": "retry_original_write"},
        ]

        def handle_request(request):
            if request.url.path == "/healthz":
                return httpx.Response(200, json={})
            return httpx.Response(409, json=responses.pop(0))

        monkeypatch.setattr(
            httpx, "AsyncClient",
            partial(httpx.AsyncClient, transport=httpx.MockTransport(handle_request)),
        )
        client = _HttpClient("https://memory.example")
        try:
            await client.initialize()
            conflict = await client.continuity_call("continuity_task", {})
            assert conflict["status"] == "rejected" and conflict["error"] == "ConflictError"
            incomplete = await client.continuity_call("add", {})
            assert incomplete["status"] == "partial"
            assert incomplete["operation_id"] == "op"
            assert incomplete["retry_action"] == "retry_original_write"
        finally:
            await client.close()
    asyncio.run(run())


def test_cancelled_initialize_cleans_up_even_when_event_loop_exits(tmp_path, monkeypatch):
    from jiuwen_memory_adapter.jiuwenswarm import agent_memory_provider as module

    entered, finish = threading.Event(), threading.Event()

    class SlowStart(Runtime):
        def start(self):
            entered.set()
            assert finish.wait(5)

    runtime = SlowStart()
    monkeypatch.setattr(module, "assemble_runtime", lambda **kwargs: runtime)
    owner = Scope(org="demo", user="owner")
    provider = AgentMemoryMemoryProvider(
        config_dict=config_for(tmp_path), continuity_enabled=True, continuity_mode="continuity",
        security_provider=lambda: internal_context(ScopedAuthenticator(owner)),
        portal={"enabled": False},
    )

    async def run():
        initializing = asyncio.create_task(provider.initialize(session_id="cancelled"))
        assert await asyncio.to_thread(entered.wait, 3)
        initializing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await initializing
        finish.set()
        # Exit immediately; cleanup cannot depend on another event-loop callback.

    asyncio.run(run())
    assert runtime.closed


def test_reads_and_writes_share_the_configured_worker_pool(tmp_path):
    async def run():
        pool, runtime = _SharedSDKRuntimePool(), Runtime()
        config = config_for(tmp_path)
        config["globals"]["sdk_max_workers"] = 4
        lease = pool.acquire(config, "owner", lambda: runtime)
        entered, finish, second_write = threading.Event(), threading.Event(), threading.Event()
        readers = threading.Barrier(2, timeout=3)

        def write():
            entered.set()
            assert finish.wait(5), "write was not released"

        def read():
            readers.wait()
            assert not finish.is_set(), "queries waited for the write"
            return "read"

        first = asyncio.create_task(pool.submit(lease, write))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            second = asyncio.create_task(pool.submit(lease, second_write.set))
            results = await asyncio.gather(
                pool.submit(lease, read),
                pool.submit(lease, read),
            )
            assert results == ["read", "read"]
            assert await asyncio.to_thread(second_write.wait, 3), "second write did not run"
        finally:
            finish.set()
            await first
            if second is not None:
                await second
            await asyncio.to_thread(pool.release, lease)
        assert runtime.closed
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
def test_interrupted_query_is_drained_before_runtime_close(tmp_path, interruption):
    entered, finish, completed = threading.Event(), threading.Event(), threading.Event()

    class CheckedRuntime(Runtime):
        def close(self, *, wait=True):
            assert completed.is_set(), "runtime closed before the query completed"
            super().close(wait=wait)

    async def run():
        pool, runtime = _SharedSDKRuntimePool(), CheckedRuntime()
        lease = pool.acquire(config_for(tmp_path), "owner", lambda: runtime)

        def read():
            entered.set()
            assert finish.wait(5), "query was not released"
            assert not runtime.closed
            completed.set()

        pending = asyncio.create_task(pool.submit(
            lease, read, timeout=0.05 if interruption == "timeout" else 120,
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if interruption == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(OperationWaitTimeout):
                    await pending
            releasing = asyncio.create_task(asyncio.to_thread(pool.release, lease))
            assert not runtime.closed
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)
            await asyncio.to_thread(pool.release, lease)
        await releasing
        assert runtime.closed
    asyncio.run(run())


@pytest.mark.parametrize("verb,operation", [
    ("search", None),
    ("continuity_task", "get"),
    ("continuity_task", "find"),
    ("continuity_task", "create"),
    ("continuity_task", "bind"),
    ("continuity_task", "unbind"),
    ("continuity_task", "attach"),
    ("continuity_task", "close"),
    ("add", None),
    ("continuity_recover", None),
])
def test_sdk_client_uses_shared_executor_for_all_operations(tmp_path, monkeypatch, verb, operation):
    from jiuwen_memory_adapter.jiuwenswarm import agent_memory_provider as module

    def invoke(**kwargs):
        return {"worker": threading.current_thread().name}

    runtime = Runtime(api=SimpleNamespace(**{verb: invoke}))
    monkeypatch.setattr(module, "assemble_runtime", lambda **kwargs: runtime)
    security = internal_context(ScopedAuthenticator(Scope(org="demo", user="owner")))

    async def run():
        provider = module.AgentMemoryMemoryProvider(
            config_dict=config_for(tmp_path), continuity_enabled=True, continuity_mode="continuity",
            security_provider=lambda: security, portal={"enabled": False},
        )
        try:
            await provider.initialize(session_id="test")
            scope = {"org": "demo", "space": "role-project"}
            if operation is not None:
                payload = {"scope": scope, "request": {
                    "operation": operation, "request_id": "request",
                }}
            elif verb == "search":
                payload = {"query": "memory", "context": {"scope": scope}}
            elif verb == "add":
                payload = {"content": "progress", "scope": scope}
            else:
                payload = {"scope": scope}
            result = await provider.continuity_request(verb, payload)
            assert result["worker"].startswith("memory-sdk_")
        finally:
            await provider.shutdown()
    asyncio.run(run())


def test_real_sdk_search_returns_background_while_another_session_is_writing(tmp_path, monkeypatch):
    async def run():
        config = config_for(tmp_path)
        owner = Scope(org="demo", user="owner")

        def security(role=Role.USER):
            return internal_context(ScopedAuthenticator(owner, role=role))

        providers = [AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            security_provider=security, portal={"enabled": False},
        ) for _ in range(2)]
        a, b = providers
        entered, finish = threading.Event(), threading.Event()
        writing = None
        try:
            await asyncio.gather(a.initialize(session_id="A"), b.initialize(session_id="B"))
            assert a.runtime is b.runtime
            api = a.runtime.api
            scope = {"org": "demo", "space": "role-project"}
            api.create_space(SpaceSpec(**scope, owner=owner), security=security(Role.ADMIN))
            task = await a.continuity_request("continuity_task", {
                "scope": scope, "request": {"operation": "create", "request_id": "create"},
            })
            bound = await a.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "bind", "request_id": "bind", "task_id": task["task_id"],
                "expected_revision": 1, "connector_id": "host", "context_id": "A",
            }})
            target = {"scope": scope, "task_id": task["task_id"],
                      "binding_id": bound["task"]["bindings"][0]["binding_id"]}
            await a.sync_turn("memory", "first section completed", event_id="first", **target)
            original_add = api.add

            def slow_add(**kwargs):
                entered.set()
                assert finish.wait(5), "write was not released"
                return original_add(**kwargs)

            monkeypatch.setattr(api, "add", slow_add)
            writing = asyncio.create_task(a.sync_turn(
                "memory", "second section completed", event_id="second", **target,
            ))
            assert await asyncio.to_thread(entered.wait, 3)
            view = await asyncio.wait_for(b.prefetch(
                "memory", scope=scope, task_id=task["task_id"],
            ), timeout=3)
            assert "first section completed" in view
            assert not writing.done(), "search waited for the write"
        finally:
            finish.set()
            if writing is not None:
                await writing
            await asyncio.gather(*(provider.shutdown() for provider in providers))
    asyncio.run(run())


def test_shared_portal_closes_once_before_the_last_runtime_release(tmp_path):
    closed = []

    class OwnedRuntime(Runtime):
        def close(self, *, wait=True):
            closed.append("runtime")
            super().close(wait=wait)

    pool, runtime = _SharedSDKRuntimePool(), OwnedRuntime()
    config = config_for(tmp_path)
    a = pool.acquire(config, "owner", lambda: runtime)
    b = pool.acquire(config, "owner", lambda: pytest.fail("runtime was not shared"))
    portal = SimpleNamespace(close=lambda: closed.append("portal"))
    assert pool.attach_portal(a, lambda borrowed: portal) is portal

    def incompatible_portal(borrowed):
        raise ValidationError("incompatible Portal options")

    with pytest.raises(ValidationError, match="incompatible"):
        pool.attach_portal(b, incompatible_portal)
    pool.release(a)
    assert not closed
    with pytest.raises(RuntimeError, match="closed"):
        pool.attach_portal(a, lambda borrowed: portal)
    pool.release(b)
    pool.release(b)
    assert closed == ["portal", "runtime"]


def test_portal_close_failure_keeps_runtime_unavailable_for_reuse(tmp_path):
    pool, runtime = _SharedSDKRuntimePool(), Runtime()
    config = config_for(tmp_path)
    lease = pool.acquire(config, "owner", lambda: runtime)

    def fail_close():
        raise RuntimeError("Portal shutdown failed")

    pool.attach_portal(lease, lambda borrowed: SimpleNamespace(close=fail_close))
    with pytest.raises(RuntimeError, match="Portal shutdown failed"):
        pool.release(lease)
    assert not runtime.closed, "runtime was closed while Portal shutdown failed"
    with pytest.raises(RuntimeError, match="cleanup failed"):
        pool.acquire(config, "owner", Runtime)


@pytest.mark.parametrize("queue_size,interruption", [(0, None), (1, "timeout"), (1, "cancel")])
def test_queue_limit_is_shared_and_interrupted_operations_keep_slots(
    tmp_path, queue_size, interruption,
):
    async def run():
        config = config_for(tmp_path)
        config["globals"].update(sdk_max_workers=1, sdk_max_queue_size=queue_size)
        pool, runtime = _SharedSDKRuntimePool(), Runtime()
        a = pool.acquire(config, "owner", lambda: runtime)
        b = pool.acquire(config, "owner", lambda: pytest.fail("runtime was not shared"))
        entered, finish, queued, rejected = [threading.Event() for _ in range(4)]

        def blocking():
            entered.set()
            assert finish.wait(5), "running operation was not released"

        running = asyncio.create_task(pool.submit(a, blocking))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if queue_size:
                if interruption == "timeout":
                    with pytest.raises(OperationWaitTimeout):
                        await pool.submit(b, queued.set, timeout=0.01)
                else:
                    waiting = asyncio.create_task(pool.submit(b, queued.set))
                    await asyncio.sleep(0)
                    waiting.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                assert not queued.is_set(), "waiting operation ran without a worker slot"
            with pytest.raises(RateLimitedError, match="queue is full"):
                await pool.submit(b, rejected.set)
            assert not rejected.is_set(), "rejected operation was executed"
            finish.set()
            await running
            if queue_size:
                assert await asyncio.to_thread(queued.wait, 3)
            assert await pool.submit(b, lambda: "capacity returned") == "capacity returned"
            assert not rejected.is_set()
        finally:
            finish.set()
            await running
            await asyncio.to_thread(pool.release, a)
            await asyncio.to_thread(pool.release, b)
    asyncio.run(run())


def test_pool_defaults_and_explicit_defaults_are_compatible(tmp_path):
    pool, runtime = _SharedSDKRuntimePool(), Runtime()
    config = config_for(tmp_path)
    a = pool.acquire(config, "owner", lambda: runtime)
    explicit = config_for(tmp_path)
    explicit["globals"].update(sdk_max_workers=16, sdk_max_queue_size=256)
    b = pool.acquire(explicit, "owner", lambda: pytest.fail("defaults did not reuse runtime"))
    try:
        assert a.entry.max_workers == 16
        assert a.entry.max_queue_size == 256
        assert "sdk_max_workers" not in config["globals"], "input config was modified"
        for key in ("sdk_max_workers", "sdk_max_queue_size"):
            changed = config_for(tmp_path)
            changed["globals"][key] = 2
            with pytest.raises(ConflictError):
                pool.acquire(changed, "owner", Runtime)
    finally:
        pool.release(a)
        pool.release(b)


@pytest.mark.parametrize("name,value", [
    ("sdk_max_workers", 0), ("sdk_max_workers", -1), ("sdk_max_workers", True),
    ("sdk_max_workers", "16"), ("sdk_max_queue_size", -1),
    ("sdk_max_queue_size", False), ("sdk_max_queue_size", 1.5),
])
def test_pool_rejects_invalid_limits_before_assembly(tmp_path, name, value):
    config = config_for(tmp_path)
    config["globals"][name] = value
    with pytest.raises(ValidationError, match=name):
        _SharedSDKRuntimePool().acquire(config, "owner", lambda: pytest.fail("runtime assembled"))


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
@pytest.mark.parametrize("outcome", ["success", "error"])
def test_actual_completion_releases_capacity_without_automatic_retry(
    tmp_path, interruption, outcome,
):
    async def run():
        config = config_for(tmp_path)
        config["globals"].update(sdk_max_workers=1, sdk_max_queue_size=0)
        pool = _SharedSDKRuntimePool()
        lease = pool.acquire(config, "owner", Runtime)
        entered, finish = threading.Event(), threading.Event()
        calls = []

        def operation():
            calls.append("called")
            entered.set()
            assert finish.wait(5), "operation was not released"
            if outcome == "error":
                raise ValueError("underlying operation failed")
            return "done"

        async def wait_for_capacity():
            while True:
                try:
                    return await pool.submit(lease, lambda: "next call")
                except RateLimitedError:
                    await asyncio.sleep(0.01)

        waiting = asyncio.create_task(pool.submit(
            lease, operation, timeout=0.05 if interruption == "timeout" else 120,
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if interruption == "cancel":
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                with pytest.raises(OperationWaitTimeout):
                    await waiting
            with pytest.raises(RateLimitedError):
                await pool.submit(lease, lambda: pytest.fail("capacity released too early"))
            finish.set()
            result = await asyncio.wait_for(wait_for_capacity(), timeout=3)
            assert calls == ["called"], "the interrupted operation was automatically retried"
            assert result == "next call"
        finally:
            finish.set()
            await asyncio.gather(waiting, return_exceptions=True)
            await asyncio.to_thread(pool.release, lease)
    asyncio.run(run())


def test_real_sdk_parallel_progress_writes_do_not_lose_task_background(tmp_path):
    async def run():
        config = config_for(tmp_path)
        owner = Scope(org="demo", user="owner")

        def security(role=Role.USER):
            return internal_context(ScopedAuthenticator(owner, role=role))

        providers = [AgentMemoryMemoryProvider(
            config_dict=config, continuity_enabled=True, continuity_mode="continuity",
            security_provider=security, portal={"enabled": False},
        ) for _ in range(2)]
        a, b = providers
        scope = {"org": "demo", "space": "role-project"}
        try:
            await asyncio.gather(a.initialize(session_id="A"), b.initialize(session_id="B"))
            a.runtime.api.create_space(
                SpaceSpec(**scope, owner=owner), security=security(Role.ADMIN),
            )
            task = await a.continuity_request("continuity_task", {
                "scope": scope, "request": {"operation": "create", "request_id": "create"},
            })
            bound = await a.continuity_request("continuity_task", {"scope": scope, "request": {
                "operation": "bind", "request_id": "bind", "task_id": task["task_id"],
                "expected_revision": 1, "connector_id": "host", "context_id": "A",
            }})
            binding_id = bound["task"]["bindings"][0]["binding_id"]
            outcomes = await asyncio.gather(*(providers[n % 2].sync_turn(
                f"work item {n}", f"completed item {n}", scope=scope,
                task_id=task["task_id"], binding_id=binding_id, event_id=f"event-{n}",
            ) for n in range(8)))
            assert all(outcome["units"] for outcome in outcomes)
            saved = await b.continuity_request("continuity_task", {
                "scope": scope, "request": {"operation": "get", "task_id": task["task_id"]},
            })
            assert saved["revision"] == 10
            assert len(saved["background"]) == 8
        finally:
            await asyncio.gather(*(p.shutdown() for p in providers))
    asyncio.run(run())
