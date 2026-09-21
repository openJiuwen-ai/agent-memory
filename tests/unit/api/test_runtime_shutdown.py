# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""真实后台任务必须先结束，Runtime 才能释放 SQLite 授权依赖。"""

from threading import Event, Thread

import pytest

from jiuwen_memory.api import (
    BackendError,
    Scope,
    assemble_runtime,
    assemble_server_runtime,
    build_configured_security_runtime,
)

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

    def scheduler_shutdown():
        events.append("scheduler")
        original_scheduler_shutdown()

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
        assert not close_done.is_set(), "runtime must wait while background users remain"
        assert events == [], "SQLite dependencies must stay open until tasks finish"
        with pytest.raises(BackendError, match="closed"):
            jobs.submit(payload_id="new", source_ref="test:new", scope=scope, task=task)
        release.set()
        assert close_done.wait(3)
        assert errors == []
        assert jobs.status(first.job.id, scope=scope).status == "succeeded"
        assert jobs.status(queued.job.id, scope=scope).status == "succeeded"
        expected = ["task", "task", "scheduler", "store", "store"]
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
