"""F07 §12.10：``MemoryRuntime`` 生命周期——start 语义与 close 组合释放。"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.api.memory_api_impl.assembly import _MemoryRuntime

pytestmark = pytest.mark.unit


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
        "storage": {
            "default": {
                "target": "composite",
                "params": {
                    "kv_store": "default",
                    "vector_store": "default",
                    "fulltext_store": "default",
                    "graph_store": "default",
                    "markdown_store": "default",
                    "shadow_index": "default",
                    "preferred_retrieval_pipeline": "recall_and_get_rank",
                },
            }
        },
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

        while runtime._kernel.watchdog._observer is None and deadline > 0:  # noqa: SLF001
            time.sleep(0.1)
            deadline -= 0.1
        assert runtime._kernel.watchdog._observer is not None  # noqa: SLF001

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
            assert runtime._kernel.watchdog._observer is not None  # noqa: SLF001
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
