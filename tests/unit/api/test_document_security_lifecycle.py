# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Document-memory lifecycle must preserve the PR2 authorization and drain barriers."""

import asyncio
from threading import Event, Thread

import pytest

from jiuwen_memory.api import (
    Context,
    DeleteSelector,
    MemoryPatch,
    PermissionDeniedError,
    Scope,
    ServerRuntime,
    ValidationError,
    assemble_runtime,
    assemble_server_runtime,
    internal_context,
)
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit


def _doc_config(root, *, watch: bool = True) -> dict:
    return {
        "globals": {
            "write_document": True,
            "watch_document": watch,
            "vector_enabled": False,
            "graph_enabled": False,
        },
        "markdown_store": {"default": {"target": "local", "params": {"root": str(root)}}},
        "shadow_index": {"default": {"target": "sqlite"}},
        "constructor": {"default": {"target": "document"}},
        "engine": {
            "default": {
                "target": "cloud",
                "params": {
                    "ingestor": "default",
                    "index_builder": "default",
                    "retriever": "default",
                    "scheduler": "default",
                    "evolver": "default",
                    "lifecycle": "default",
                    "job_factory": "default",
                },
            }
        },
    }


@pytest.mark.parametrize("kind", ["sdk", "server"])
def test_document_ingest_drains_before_shadow_close(tmp_path, monkeypatch, kind) -> None:
    """
    A queued ingest task must retain its real shadow connection until it finishes.
    """
    # pylint: disable=protected-access
    build = assemble_runtime if kind == "sdk" else assemble_server_runtime
    runtime = build(config=_doc_config(tmp_path, watch=False))
    jobs = runtime.api._ingest_jobs
    kernel = runtime._kernel if kind == "sdk" else runtime._runtime._kernel
    shadow = kernel.storage.shadow_index()
    started = Event()
    release = Event()
    drain = Event()
    done = Event()
    errors = []
    events = []
    original_jobs_close = jobs.close
    original_shadow_close = shadow.close

    def close_jobs(*, wait=True):
        if wait:
            drain.set()
        original_jobs_close(wait=wait)

    def close_shadow():
        events.append("shadow")
        original_shadow_close()

    def task():
        started.set()
        assert release.wait(5), "test must release ingest"
        shadow.health()
        events.append("task")
        return []

    def close_runtime():
        try:
            runtime.close(wait=False)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(jobs, "close", close_jobs)
    monkeypatch.setattr(shadow, "close", close_shadow)
    closer = Thread(target=close_runtime)
    try:
        jobs.submit(payload_id="document", source_ref="test:document", scope=Scope(), task=task)
        assert started.wait(2)
        closer.start()
        assert drain.wait(2)
        assert done.wait(2), "wait=False must return without releasing a live shadow dependency"
        shadow.health()
        assert events == []
        release.set()
        runtime.close(wait=True)
        assert errors == []
        assert events == ["task", "shadow"]
        runtime.close()
        assert events == ["task", "shadow"], "shadow must close once"
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(3)
        runtime.close()


def test_watchdog_worker_drains_before_shared_security_close(tmp_path, monkeypatch) -> None:
    """
    Closing a shared security view must also wait for a running watcher worker.
    """
    # pylint: disable=protected-access
    config = _doc_config(tmp_path)
    config["security"] = {
        "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
    }
    runtime = assemble_runtime(config=config)
    watchdog = runtime._kernel.watchdog
    shadow = runtime._kernel.storage.shadow_index()
    started = Event()
    release = Event()
    stopping = Event()
    done = Event()
    errors = []
    calls = []
    original_stop = watchdog.stop
    original_close = shadow.close

    def sync_file(filename, deleted):
        started.set()
        assert release.wait(5)
        shadow.health()
        calls.append("worker")

    def stop():
        stopping.set()
        original_stop()

    def close_shadow():
        calls.append("shadow")
        original_close()

    def close_security():
        try:
            runtime._kernel.security_runtime.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(watchdog, "_sync_one", sync_file)
    monkeypatch.setattr(watchdog, "stop", stop)
    monkeypatch.setattr(shadow, "close", close_shadow)
    worker = Thread(target=watchdog._run_sync, args=("memory/USER.md", False, 0))
    closer = Thread(target=close_security)
    try:
        worker.start()
        assert started.wait(2)
        closer.start()
        assert stopping.wait(2)
        assert not done.is_set()
        assert calls == []
        release.set()
        assert done.wait(3)
        assert errors == []
        assert calls == ["worker", "shadow"]
        watchdog._run_sync("memory/USER.md", False, 0)
        assert calls == ["worker", "shadow"], "closed or stale workers cannot enter shadow"
    finally:
        release.set()
        worker.join(3)
        if closer.ident is not None:
            closer.join(3)
        runtime.close()


def test_server_facade_exposes_upstream_start_without_raw_ports(tmp_path) -> None:
    """
    The public Server facade forwards the upstream lifecycle without exposing stores.
    """
    runtime = assemble_server_runtime(config=_doc_config(tmp_path))
    try:
        assert isinstance(runtime, ServerRuntime)
        runtime.start()
        for port in ("kv", "storage", "watchdog"):
            assert not hasattr(runtime, port)
    finally:
        runtime.close()
    with pytest.raises(ValidationError, match="closed"):
        runtime.start()
    with pytest.raises(ValidationError, match="closed"):
        asyncio.run(runtime.start_background())


def test_watchdog_start_failure_reaches_sync_caller(tmp_path, monkeypatch) -> None:
    """
    A failed observer start cannot be reported as a successful Runtime startup.
    """
    # pylint: disable=protected-access
    runtime = assemble_runtime(config=_doc_config(tmp_path))

    def fail_start():
        raise ValidationError("observer startup failure")

    monkeypatch.setattr(runtime._kernel.watchdog, "start", fail_start)
    try:
        with pytest.raises(ValidationError, match="observer startup failure"):
            runtime.start()
    finally:
        runtime.close()


def test_document_memory_uses_real_scope_and_authorization(tmp_path) -> None:
    """
    Document CRUD and retrieval must continue through the PR2 PEP with real facts.
    """
    runtime = assemble_runtime(config=_doc_config(tmp_path, watch=False))
    scope = Scope(org="acme", user="alice")
    alice = internal_context(ScopedAuthenticator(scope))
    bob = internal_context(ScopedAuthenticator(Scope(org="acme", user="bob")))
    try:
        unit = runtime.api.add("zephyr secret", scope, security=alice)[0]
        assert runtime.api.get(unit.id, scope, security=alice).id == unit.id
        assert runtime.api.list(scope, security=alice).count == 1
        assert runtime.api.search("zephyr", Context(scope=scope), security=alice).items
        with pytest.raises(PermissionDeniedError):
            runtime.api.get(unit.id, scope, security=bob)
        updated = runtime.api.update(
            unit.id, scope, MemoryPatch(content="zephyr revised"), security=alice
        )
        assert updated.content == "zephyr revised"
        assert runtime.api.delete(
            DeleteSelector(scope=scope, unit_ids=[updated.id]), security=alice
        )
    finally:
        runtime.close()
