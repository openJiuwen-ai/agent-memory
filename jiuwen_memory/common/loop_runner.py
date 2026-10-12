# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""可关闭的专职事件循环线程——从其他线程提交协程到常驻 loop 执行。

解决"sync 调用方经 ``asyncio.run`` 建临时 loop，返回即关闭，绑上去的
后台 task 全部随 loop 消亡"的问题：协程提交到本 Runner 拥有的常驻
loop（daemon 线程 ``run_forever``），不再依赖任何一次调用的临时 loop。
资源所有者可通过 ``close`` 确定性地结束线程。

多个调用线程可并发提交（``run_coroutine_threadsafe`` 天然支持）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any


class LoopRunner:
    """可关闭的专职事件循环线程。

    ``run`` 可从任意线程调用：协程经 ``run_coroutine_threadsafe`` 提交到常驻
    loop，调用线程阻塞等待结果；``ensure_loop`` 取常驻 loop 供调用方自行
    ``run_coroutine_threadsafe`` 提交。多个调用线程可并发提交。
    所有者应先结束自己的业务任务，再调用 ``close``；剩余协程会被取消，
    异步生成器、默认 executor 与 loop 随线程一起清理。
    """

    def __init__(self, thread_name: str = "agent-memory-loop") -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        self._closed = False

    def in_loop_thread(self) -> bool:
        """调用方是否位于本 Runner 的线程（该线程不能同步等待自身）。"""
        return threading.current_thread() is self._thread

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        """返回常驻 loop——不存在或线程已死时（惰性）重建。"""
        with self._lock:
            return self._ensure_loop_locked()

    def _ensure_loop_locked(self) -> asyncio.AbstractEventLoop:
        if self._closed:
            raise RuntimeError("loop runner is closed")
        if self._thread is not None and self._thread.is_alive():
            if self._loop is None:
                raise RuntimeError(
                    f"loop thread {self._thread_name!r} is alive but event loop is missing"
                )
            return self._loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, args=(self._loop,), name=self._thread_name, daemon=True
        )
        self._thread.start()
        return self._loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                tasks = asyncio.all_tasks(loop)
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """与 close 原子排序地接纳协程；拒绝时释放尚未 await 的协程。"""
        with self._lock:
            try:
                return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop_locked())
            except BaseException:
                coro.close()
                raise

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """提交协程到常驻 loop 并阻塞等待结果——只能从本 loop 之外调用。"""
        if self.in_loop_thread():
            coro.close()
            raise RuntimeError("cannot synchronously wait on the loop runner's own thread")
        return self.submit(coro).result(timeout)

    def close(self, *, wait: bool = True) -> None:
        """停止 loop 并释放线程；幂等，关闭后不可重启。wait=False 不等待清理完成。"""
        if wait and self.in_loop_thread():
            raise RuntimeError("cannot join the loop runner's own thread")
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._loop is not None and self._thread is not None and self._thread.is_alive():
                    self._loop.call_soon_threadsafe(self._loop.stop)
            thread = self._thread
        if wait and thread is not None:
            thread.join()
