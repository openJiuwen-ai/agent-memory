"""F07 §12.10：``MemoryRuntime`` 生命周期——start 语义与 close 组合释放。"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为
import asyncio
import tempfile
import threading
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.api.memory_api_impl.assembly import _Kernel, _MemoryRuntime
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import AsyncTimerScheduler
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failed_step", ["watchdog", "scheduler_start", "ingest_start"])
def test_close_start_failure_does_not_skip_other_cleanup(failed_step: str) -> None:
    events: list[str] = []

    def step(name: str) -> None:
        events.append(name)
        if name == failed_step and events.count(name) == 1:
            raise RuntimeError(name)

    class RecordingScheduler(InProcessScheduler):
        def shutdown(self, *, wait: bool = True) -> None:
            step("scheduler_wait" if wait else "scheduler_start")

    runtime = _MemoryRuntime(
        api=SimpleNamespace(release_dreaming_lock=lambda: step("leader")),
        _kernel=SimpleNamespace(
            watchdog=SimpleNamespace(stop=lambda: step("watchdog")),
            scheduler=RecordingScheduler(),
            ingest_jobs=SimpleNamespace(
                close=lambda wait=True: step("ingest_wait" if wait else "ingest_start")
            ),
            close=lambda: step("resources"),
        ),
    )
    with pytest.raises(RuntimeError, match=failed_step):
        runtime.close()
    assert not runtime._close_thread.is_alive(), "wait=True must finish safe cleanup before raising"
    assert {"scheduler_wait", "ingest_wait", "leader", "resources"} <= set(events)
    assert events.index("leader") > events.index("scheduler_wait")
    before = list(events)
    with pytest.raises(RuntimeError, match=failed_step):
        runtime.close()
    assert events == before, "repeated close must re-report failure without repeating cleanup"


@pytest.mark.parametrize("failed_step", ["watchdog", "scheduler", "ingest"])
def test_unconfirmed_stop_preserves_lock_and_storage(failed_step: str) -> None:
    events: list[str] = []

    def step(name: str) -> None:
        events.append(name)
        if name == failed_step:
            raise RuntimeError(name)

    scheduler = SimpleNamespace(
        validate_shutdown=lambda wait=True: None,
        shutdown=lambda wait=True: step("scheduler"),
    )
    runtime = _MemoryRuntime(
        api=SimpleNamespace(release_dreaming_lock=lambda: step("leader")),
        _kernel=SimpleNamespace(
            watchdog=SimpleNamespace(stop=lambda: step("watchdog")),
            scheduler=scheduler,
            ingest_jobs=SimpleNamespace(close=lambda wait=True: step("ingest")),
            close=lambda: step("resources"),
        ),
    )
    runtime.close(wait=False)
    with pytest.raises(ExceptionGroup) as caught:
        runtime.close()
    assert all(str(exc) == failed_step for exc in caught.value.exceptions)
    assert {"watchdog", "scheduler", "ingest"} <= set(events)
    assert "leader" not in events, "failed stop must not allow a new leader to take over"
    assert "resources" not in events, "potentially active tasks must retain their storage"
    assert not runtime._close_thread.is_alive()


def test_lock_release_failure_still_closes_shadow_and_reports_all_errors() -> None:
    events: list[str] = []

    def fail(name: str) -> None:
        events.append(name)
        raise RuntimeError(name)

    shadow = SimpleNamespace(close=lambda: fail("shadow"))
    api = SimpleNamespace(release_dreaming_lock=lambda: fail("leader"))
    kernel = _Kernel(
        api=api, kv=None,
        storage=SimpleNamespace(has_shadow_index=lambda: True, shadow_index=lambda: shadow),
        ingest_jobs=SimpleNamespace(close=lambda wait=True: None),
    )
    runtime = _MemoryRuntime(api=api, _kernel=kernel)
    with pytest.raises(ExceptionGroup) as caught:
        runtime.close()
    assert events == ["leader", "shadow"], "independent safe cleanup must continue after failure"
    assert [str(exc) for exc in caught.value.exceptions] == ["leader", "shadow"]
    assert not runtime._close_thread.is_alive()


def test_runtime_close_cannot_wait_on_scheduler_own_loop() -> None:
    runtime = assemble_runtime(config={"scheduler": {"default": {"target": "async_timer"}}})
    scheduler = runtime._kernel.scheduler

    async def attempt() -> None:
        with pytest.raises(RuntimeError, match="own loop"):
            runtime.close()
        assert runtime._close_thread is None, "invalid context must fail before changing lifecycle"

    try:
        scheduler._runner.run(attempt(), timeout=3)
    finally:
        runtime.close()


def test_failed_loop_stop_does_not_join_a_potentially_running_thread() -> None:
    events: list[str] = []

    def stop(callback) -> None:
        events.append("stop")
        raise RuntimeError("stop failed")

    runtime = _MemoryRuntime(
        api=SimpleNamespace(),
        _kernel=SimpleNamespace(
            watchdog=None, scheduler=None,
            ingest_jobs=SimpleNamespace(close=lambda wait=True: None),
            close=lambda: events.append("resources"),
        ),
    )
    loop = SimpleNamespace(is_closed=lambda: False, stop=lambda: None, call_soon_threadsafe=stop)
    runtime._loop = loop
    runtime._loop_thread = SimpleNamespace(join=lambda: events.append("join"))
    with pytest.raises(RuntimeError, match="stop failed"):
        runtime.close()
    assert events == ["resources", "stop"], "failed stop must not block forever in thread.join"
    assert runtime._loop is loop, "retain the loop reference when its stop is unconfirmed"


def test_nonblocking_close_keeps_resources_and_leader_until_job_finishes() -> None:
    started, release = threading.Event(), threading.Event()
    events: list[str] = []

    class BlockingJob(Job):
        async def run(self) -> JobInfo:
            started.set()
            await asyncio.to_thread(release.wait, 10)
            events.append("job_finished")
            self.check_cancelled()
            return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED)

    scheduler = AsyncTimerScheduler(tick_interval=1)
    kernel = SimpleNamespace(
        watchdog=None,
        scheduler=scheduler,
        ingest_jobs=InProcessIngestJobController(),
        close=lambda: events.append("resources_closed"),
    )
    api = SimpleNamespace(release_dreaming_lock=lambda: events.append("leader_released"))
    runtime = _MemoryRuntime(api=api, _kernel=kernel)
    try:
        job_id = asyncio.run(scheduler.submit(BlockingJob(), Channel.BACKGROUND))
        assert started.wait(3)
        runtime.close(wait=False)
        assert not events, "in-flight work must keep its storage and leader lock"
        assert runtime._close_thread.is_alive()
        release.set()
        runtime.close(wait=True)
        assert events == ["job_finished", "leader_released", "resources_closed"]
        assert not scheduler._runner._thread.is_alive()
        assert scheduler.status(job_id).status == JobStatus.CANCELLED
        runtime.close()
        assert events == ["job_finished", "leader_released", "resources_closed"]
    finally:
        release.set()
        runtime.close()


def test_assembled_runtimes_have_independent_scheduler_lifetimes() -> None:
    config = {"scheduler": {"default": {"target": "async_timer"}}}
    first, second = assemble_runtime(config=config), assemble_runtime(config=config)

    class Once(Job):
        async def run(self) -> JobInfo:
            return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED)

    try:
        assert first._kernel.scheduler is not second._kernel.scheduler
        asyncio.run(first._kernel.scheduler.submit(Once(), Channel.BACKGROUND))
        first.close()
        job_id = asyncio.run(second._kernel.scheduler.submit(Once(), Channel.BACKGROUND))
        second.close()
        assert second._kernel.scheduler.status(job_id).status in {
            JobStatus.SUCCEEDED, JobStatus.CANCELLED,
        }
        for runtime in (first, second):
            assert not runtime._kernel.scheduler._runner._thread.is_alive()
    finally:
        first.close()
        second.close()


def test_close_without_wait_can_later_wait_for_ingest_pool() -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    controller = InProcessIngestJobController()

    def task() -> list:
        started.set()
        assert release.wait(10)
        finished.set()
        return []

    try:
        controller.submit(payload_id="p", source_ref="s", scope=Scope(), task=task)
        assert started.wait(3)
        controller.close(wait=False)
        release.set()
        controller.close(wait=True)
        assert finished.is_set(), "a later wait=True must join a previously closed pool"
    finally:
        release.set()
        controller.close()


def _doc_config(tmp: str) -> dict:
    """最小文档模式配置（离线：无 embedder/ES，md + shadow sqlite 落临时目录）。"""
    return {
        "globals": {
            "write_document": True,
            "watch_document": True,
            "vector_enabled": False,
        },
        "markdown_store": {"default": {"target": "local", "params": {"root": tmp}}},
        "shadow_index": {"default": {"target": "sqlite", "params": {}}},
        # F07 起 Storage 拆为 StoreManager（管理面）+ DomainStore（数据面）：顶层配置段
        # 从 storage: 更名 store_manager:，数据面装配参数全住 domain_stores.<name>；
        # 七类 Store 端口由 manager 扫 *_store 命名空间聚合（kv_store.default 走 defaults）。
        "store_manager": {
            "default": {
                "target": "composite",
                "params": {
                    "domain_stores": {
                        "default": {
                            "kv_store": "default",
                            # 文档模式召回统一走 ShadowRecaller（shadow.search_fulltext +
                            # search_vector 复合算子），替代 KV 时代的 keyword/vector 各路。
                            "shadow_recaller": "shadow",
                            "preferred_retrieval_pipeline": "recall_and_get_rank",
                        },
                    },
                },
            },
        },
        # 文档模式唯一召回通道（影子索引召回算子）。
        "recaller": {"shadow": {"target": "shadow"}},
    }


def test_runtime_close_without_start_is_safe() -> None:
    """未调 start 直接 close（既有调用方语义）：不抛错。"""
    runtime = assemble_runtime(config={})
    runtime.close(wait=False)


def test_start_no_watchdog_is_noop_and_idempotent() -> None:
    """默认（非文档模式）无看门狗：start / start_background 均为 no-op，可重复调。"""
    runtime = assemble_runtime(config={})
    runtime.start()
    runtime.start()  # 幂等
    asyncio.run(runtime.start_background())
    runtime.close(wait=False)


def test_sync_start_boots_watchdog_in_loop_thread_and_close_stops_it() -> None:
    """同步面 start：daemon 线程跑专属 loop，看门狗在其中 start；close 停止两者。"""
    with tempfile.TemporaryDirectory() as tmp:
        runtime = assemble_runtime(config=_doc_config(tmp))
        assert isinstance(runtime, _MemoryRuntime)
        assert runtime._kernel.watchdog is not None

        runtime.start()
        thread = runtime._loop_thread
        assert thread is not None and thread.is_alive()
        # 看门狗 Observer 已在专属 loop 里 start（_observer 就位）
        deadline = 50.0
        import time

        while runtime._kernel.watchdog._observer is None and deadline > 0:
            time.sleep(0.1)
            deadline -= 0.1
        assert runtime._kernel.watchdog._observer is not None

        runtime.close(wait=False)
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        runtime.close(wait=False)  # 幂等


def test_async_start_background_boots_watchdog_in_current_loop() -> None:
    """异步面 start_background：看门狗绑当前 loop（call_soon_threadsafe 可达）。"""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = assemble_runtime(config=_doc_config(tmp))
            await runtime.start_background()
            assert runtime._kernel.watchdog is not None
            assert runtime._kernel.watchdog._observer is not None
            runtime.close(wait=False)

    asyncio.run(scenario())


def test_close_leaks_no_public_ports() -> None:
    """生命周期方法不把 kernel 端口暴露到公开面（S02 边界）。"""
    runtime = assemble_runtime(config={})
    for port in ("kv", "storage", "space", "ingest_jobs", "watchdog"):
        assert not hasattr(runtime, port), f"runtime leaked {port}"
    assert hasattr(runtime, "api")
    assert callable(runtime.start)
    assert callable(runtime.close)
    runtime.close(wait=False)
