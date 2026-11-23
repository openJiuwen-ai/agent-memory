# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""BlockingExecutor 契约测试（F10 决策 1）。

覆盖重入矩阵、上下文传播、关闭语义和「不是进程级单例」。异步用例统一用
``asyncio.run`` 驱动：仓库当前没有 pytest-asyncio，``@pytest.mark.asyncio`` 的
async def 用例不会真正执行。
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.control.executor import BlockingExecutorProducer, ExecutorLane
from jiuwen_memory.control.executor_impl.thread_pool import ThreadPoolBlockingExecutor

pytestmark = pytest.mark.unit


def test_run_propagates_contextvars() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="missing")
    token = marker.set("from-caller")
    executor = ThreadPoolBlockingExecutor(llm_workers=1, io_workers=1)
    try:
        seen = asyncio.run(executor.run(ExecutorLane.IO, marker.get))
    finally:
        marker.reset(token)
        executor.close()
    assert seen == "from-caller", "线程内必须能读到提交方设置的 ContextVar"


def test_reentry_matrix() -> None:
    executor = ThreadPoolBlockingExecutor(llm_workers=1, io_workers=1)

    def nested(lane: ExecutorLane) -> int:
        async def inner() -> int:
            return await executor.run(lane, threading.get_ident)

        return asyncio.run(inner())

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        io_thread = await executor.run(ExecutorLane.IO, threading.get_ident)
        llm_thread = await executor.run(ExecutorLane.LLM, threading.get_ident)
        assert io_thread != loop_thread, "事件循环提交的 IO 任务必须换线程"
        assert llm_thread != loop_thread, "事件循环提交的 LLM 任务必须换线程"
        assert await executor.run(ExecutorLane.IO, nested, ExecutorLane.IO) == io_thread, (
            "io 工作线程提交 io 应在当前线程内联"
        )
        assert await executor.run(ExecutorLane.IO, nested, ExecutorLane.LLM) == llm_thread, (
            "io 工作线程提交 llm 应排队到 llm 池"
        )
        assert await executor.run(ExecutorLane.LLM, nested, ExecutorLane.IO) == llm_thread, (
            "llm 工作线程提交 io 应在当前线程内联"
        )
        assert await executor.run(ExecutorLane.LLM, nested, ExecutorLane.LLM) == llm_thread, (
            "llm 工作线程提交 llm 应在当前线程内联"
        )

    try:
        asyncio.run(scenario())
    finally:
        executor.close()


def test_nested_bridge_completes_with_single_thread_pools() -> None:
    executor = ThreadPoolBlockingExecutor(llm_workers=1, io_workers=1)

    def nested() -> tuple[str, str]:
        async def inner() -> tuple[str, str]:
            first = await executor.run(ExecutorLane.IO, lambda: "io")
            second = await executor.run(ExecutorLane.LLM, lambda: "llm")
            return first, second

        return asyncio.run(inner())

    async def scenario() -> tuple[str, str]:
        return await asyncio.wait_for(executor.run(ExecutorLane.IO, nested), timeout=5)

    try:
        assert asyncio.run(scenario()) == ("io", "llm"), "嵌套桥接在单线程池下必须能完成"
    finally:
        executor.close()


def test_close_waits_running_then_rejects() -> None:
    executor = ThreadPoolBlockingExecutor(llm_workers=1, io_workers=1)
    started = threading.Event()
    finished = threading.Event()

    def work() -> None:
        started.set()
        time.sleep(0.2)
        finished.set()

    async def scenario() -> None:
        task = asyncio.ensure_future(executor.run(ExecutorLane.IO, work))
        await asyncio.to_thread(started.wait, 2)
        assert not finished.is_set(), "前置条件：任务应仍在运行"
        executor.close(wait=True)
        assert finished.is_set(), "close(wait=True) 返回前运行中的任务必须结束"
        await task
        with pytest.raises(RuntimeError, match="executor closed"):
            await executor.run(ExecutorLane.IO, lambda: None)

    try:
        asyncio.run(scenario())
    finally:
        executor.close()


def test_two_assemblies_get_distinct_instances() -> None:
    spec = {"executor": {"default": {"target": "thread_pool", "params": {}}}}
    first = BlockingExecutorProducer.build(
        "thread_pool", {}, AssemblyContext.from_dict(spec)
    )
    second = BlockingExecutorProducer.build(
        "thread_pool", {}, AssemblyContext.from_dict(spec)
    )
    try:
        assert first is not second, "执行器归装配上下文所有，不能是进程级单例"
    finally:
        first.close()
        second.close()
