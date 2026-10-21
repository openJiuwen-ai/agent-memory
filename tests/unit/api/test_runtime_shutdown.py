# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实后台任务必须先结束，Runtime 才能释放 SQLite 授权依赖。"""

import asyncio
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import (
    BackendError,
    Scope,
    assemble_runtime,
    assemble_server_runtime,
    build_configured_security_runtime,
)
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["sdk", "secured_sdk", "server", "explicit_server"])
def test_runtime_nonwaiting_request_still_drains_before_sqlite_close(monkeypatch, kind) -> None:
    # 白盒仅取得本次真实装配的控制器和 Store，不增加 Access 私有依赖。
    # pylint: disable=protected-access
    config = {
        "grant_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
        "delegation_store": {"default": {"target": "sqlite", "params": {"db_path": ":memory:"}}},
    }
    if kind != "sdk":
        config["security"] = {
            "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
        }
    injected = build_configured_security_runtime(config) if kind == "explicit_server" else None
    if kind == "explicit_server":
        runtime = assemble_server_runtime(config=config, security_runtime=injected)
    elif kind == "server":
        runtime = assemble_server_runtime(config=config)
    else:
        runtime = assemble_runtime(config=config)
    jobs = runtime.api._ingest_jobs
    authorizer = runtime.api._authorizer
    stores = (authorizer.management_grant_store(), *authorizer.delegation_stores())
    started = Event()
    release = Event()
    drain_started = Event()
    close_done = Event()
    events = []
    errors = []
    original_jobs_close = jobs.close
    original_scheduler_shutdown = runtime.api._scheduler.shutdown

    def jobs_close(*, wait=True):
        if wait:
            drain_started.set()
        original_jobs_close(wait=wait)

    def tracked_close(store):
        original = store.close

        def close():
            events.append("store")
            original()

        return close

    def scheduler_shutdown(*, wait=True):
        events.append("scheduler" if wait else "scheduler_requested")
        original_scheduler_shutdown(wait=wait)

    def task():
        started.set()
        assert release.wait(5), "test must release the running task"
        for store in stores:
            store.health()
        events.append("task")
        return []

    def close_runtime():
        try:
            runtime.close(wait=False)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_done.set()

    monkeypatch.setattr(jobs, "close", jobs_close)
    monkeypatch.setattr(runtime.api._scheduler, "shutdown", scheduler_shutdown)
    for store in stores:
        monkeypatch.setattr(store, "close", tracked_close(store))
    closer = Thread(target=close_runtime)
    scope = Scope(org="acme", user="alice")
    try:
        first = jobs.submit(payload_id="running", source_ref="test:running", scope=scope, task=task)
        assert started.wait(2)
        queued = jobs.submit(payload_id="queued", source_ref="test:queued", scope=scope, task=task)
        closer.start()
        assert drain_started.wait(2), "wait=False must not disable the final wait=True barrier"
        assert close_done.wait(2), "wait=False must return while background users remain"
        assert events == ["scheduler_requested"], "SQLite must stay open until tasks finish"
        for store in stores:
            store.health()
        with pytest.raises(BackendError, match="closed"):
            jobs.submit(payload_id="new", source_ref="test:new", scope=scope, task=task)
        release.set()
        runtime.close(wait=True)
        assert errors == []
        assert jobs.status(first.job.id, scope=scope).status == "succeeded"
        assert jobs.status(queued.job.id, scope=scope).status == "succeeded"
        expected = ["scheduler_requested", "task", "task", "scheduler", "store", "store"]
        assert events == expected
        runtime.close(wait=True)
        assert events == expected, "shared close must be idempotent"
        for store in stores:
            with pytest.raises(BackendError):
                store.health()
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(timeout=3)
        runtime.close()
        if injected is not None:
            injected.close()


def test_shared_close_and_ingest_nonblocking_close_do_not_deadlock(monkeypatch) -> None:
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={
            "security": {
                "default": {"target": "standard", "params": {"authenticator": {"target": "dev"}}}
            },
            "grant_store": {"default": {"target": "sqlite"}},
            "delegation_store": {"default": {"target": "sqlite"}},
        }
    )
    jobs = runtime.api._ingest_jobs
    security_runtime = runtime._kernel.security_runtime
    store = runtime.api._authorizer.management_grant_store()
    started, drain, shared_done, task_done = Event(), Event(), Event(), Event()
    errors = []
    original = jobs.close

    def close_jobs(*, wait=True):
        if wait:
            drain.set()
        original(wait=wait)

    def task():
        started.set()
        assert drain.wait(5), "shared close must enter the ingest barrier"
        runtime.close(wait=False)
        store.health()
        task_done.set()
        return []

    def close_security():
        try:
            security_runtime.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            shared_done.set()

    monkeypatch.setattr(jobs, "close", close_jobs)
    closer = Thread(target=close_security, daemon=True)
    try:
        jobs.submit(payload_id="shared", source_ref="test:shared", scope=Scope(), task=task)
        assert started.wait(2)
        closer.start()
        assert task_done.wait(3), "nonblocking close must not wait for the owner's drain lock"
        assert shared_done.wait(3), "shared close must not join the resource finisher"
        runtime.close(wait=True)
        assert errors == []
        with pytest.raises(BackendError):
            store.health()
    finally:
        drain.set()
        closer.join(timeout=5)
        runtime.close()


def test_scheduled_job_keeps_sqlite_until_cooperative_shutdown() -> None:
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={
            "scheduler": {"default": {"target": "async_timer"}},
            "grant_store": {"default": {"target": "sqlite"}},
            "delegation_store": {"default": {"target": "sqlite"}},
        }
    )
    scheduler = runtime._kernel.scheduler
    stores = (
        runtime.api._authorizer.management_grant_store(),
        *runtime.api._authorizer.delegation_stores(),
    )
    started, release, finished = Event(), Event(), Event()

    class ReadingJob(Job):
        async def run(self) -> JobInfo:
            started.set()
            assert await asyncio.to_thread(release.wait, 5)
            for store in stores:
                store.health()
            finished.set()
            self.check_cancelled()
            return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED)

    try:
        job_id = asyncio.run(scheduler.submit(ReadingJob(), Channel.BACKGROUND))
        assert started.wait(2)
        runtime.close(wait=False)
        assert not finished.is_set()
        for store in stores:
            store.health()
        release.set()
        runtime.close(wait=True)
        assert finished.is_set(), "scheduled code must finish before SQLite is released"
        assert scheduler.status(job_id).status == JobStatus.CANCELLED
        assert not scheduler._runner._thread.is_alive()
        for store in stores:
            with pytest.raises(BackendError):
                store.health()
    finally:
        release.set()
        runtime.close()


def test_background_resource_failure_is_reported_without_repeating_cleanup(monkeypatch) -> None:
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={
            "grant_store": {"default": {"target": "sqlite"}},
            "delegation_store": {"default": {"target": "sqlite"}},
        }
    )
    grants = runtime.api._authorizer.management_grant_store()
    delegations = runtime.api._authorizer.delegation_stores()
    original = grants.close
    attempts = []

    def fail_close():
        attempts.append(True)
        raise BackendError("injected SQLite close failure")

    monkeypatch.setattr(grants, "close", fail_close)
    try:
        runtime.close(wait=False)
        with pytest.raises(BackendError, match="SQLite close failure"):
            runtime.close(wait=True)
        assert len(attempts) == 1
        with pytest.raises(BackendError, match="SQLite close failure"):
            runtime.close(wait=True)
        assert len(attempts) == 1, "wait=True must re-report the same cleanup result"
        for store in delegations:
            with pytest.raises(BackendError):
                store.health()
    finally:
        original()


def test_close_does_not_release_real_sqlite_when_ingest_stop_is_unconfirmed(monkeypatch) -> None:
    # pylint: disable=protected-access
    runtime = assemble_runtime(
        config={
            "grant_store": {"default": {"target": "sqlite"}},
            "delegation_store": {"default": {"target": "sqlite"}},
        }
    )
    jobs = runtime.api._ingest_jobs
    stores = (
        runtime.api._authorizer.management_grant_store(),
        *runtime.api._authorizer.delegation_stores(),
    )
    original = jobs.close

    def fail_stop(*, wait=True):
        if not wait:
            original(wait=False)
        raise BackendError("cannot confirm ingest stopped")

    monkeypatch.setattr(jobs, "close", fail_stop)
    try:
        runtime.close(wait=False)
        with pytest.raises(ExceptionGroup):
            runtime.close(wait=True)
        for store in stores:
            store.health()
        with pytest.raises(ExceptionGroup):
            runtime.security_resource_owner().close()
        for store in stores:
            store.health()
    finally:
        original(wait=True)
        for store in stores:
            store.close()


def test_injected_close_only_combination_is_deferred_until_ingest_finishes() -> None:
    # pylint: disable=protected-access
    selected_closed = Event()
    selected = SimpleNamespace(close=selected_closed.set)
    runtime = assemble_server_runtime(config={}, security_runtime=selected)
    jobs = runtime.api._ingest_jobs
    started, release, finished = Event(), Event(), Event()

    def task():
        started.set()
        assert release.wait(5)
        assert not selected_closed.is_set(), "Server must retain the injected combination"
        finished.set()
        return []

    try:
        jobs.submit(payload_id="opaque", source_ref="test:opaque", scope=Scope(), task=task)
        assert started.wait(2)
        runtime.close(wait=False)
        assert not selected_closed.is_set()
        release.set()
        runtime.close(wait=True)
        assert finished.is_set()
        assert selected_closed.is_set()
    finally:
        release.set()
        runtime.close()
