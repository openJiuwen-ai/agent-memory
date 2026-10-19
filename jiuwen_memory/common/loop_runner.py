# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""进程级专职事件循环线程——任意线程提交协程到常驻 loop 执行。

解决"sync 调用方经 ``asyncio.run`` 建临时 loop，返回即关闭，绑上去的
后台 task 全部随 loop 消亡"的问题：协程提交到本 Runner 拥有的常驻
loop（daemon 线程 ``run_forever``），生命周期由持有者显式关闭，不再依赖
任何一次调用的临时 loop。

多个调用线程可并发提交（``run_coroutine_threadsafe`` 天然支持）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class LoopRunner:
    """进程级专职事件循环线程。

    ``run`` 可从任意线程调用：协程经 ``run_coroutine_threadsafe`` 提交到常驻
    loop，调用线程阻塞等待结果；``ensure_loop`` 取常驻 loop 供调用方自行
    ``run_coroutine_threadsafe`` 提交。多个调用线程可并发提交。
    ``close`` 停止循环并回收异步生成器、默认线程池与线程；关闭后不得重启。
    业务资源（如连接池）仍由调用方在停止循环前释放。
    """

    def __init__(self, thread_name: str = "agent-memory-loop") -> None:
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        self._closed = False

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        """惰性创建常驻 loop；关闭后拒绝创建或重启。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("LoopRunner is closed")
            if self._thread is not None and self._thread.is_alive():
                if self._loop is None:
                    raise RuntimeError(
                        f"loop thread {self._thread_name!r} is alive but event loop is missing"
                    )
                return self._loop
            ready = threading.Event()
            startup_errors: list[Exception] = []
            self._loop = None
            self._thread = threading.Thread(
                name=self._thread_name,
                daemon=True,
                target=self._run_loop,
                args=(ready, startup_errors),
            )
            self._thread.start()
            # 循环在所属线程内创建；就绪信号同时覆盖初始化失败，避免调用方永等。
            ready.wait()
            if startup_errors:
                self._thread.join()
                raise RuntimeError("LoopRunner failed to start") from startup_errors[0]
            if self._loop is None:
                raise RuntimeError("LoopRunner started without an event loop")
            return self._loop

    def _run_loop(self, ready: threading.Event, startup_errors: list[Exception]) -> None:
        """Runner 管理循环资源；外层线程池上下文确保所有同步调用真正退出。"""
        try:
            with (
                ThreadPoolExecutor(thread_name_prefix=self._thread_name) as executor,
                asyncio.Runner() as runtime,
            ):
                loop = runtime.get_loop()
                loop.set_default_executor(executor)
                self._loop = loop
                ready.set()
                loop.run_forever()
        except Exception as exc:
            if ready.is_set():
                raise
            startup_errors.append(exc)
        finally:
            ready.set()

    def close(self, *, wait: bool = True) -> None:
        """幂等停止；wait=True 等待线程池及循环线程退出，禁止线程等待自身。"""
        if wait and self.in_loop_thread():
            raise RuntimeError("cannot wait for LoopRunner from its own thread")
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._loop is not None and not self._loop.is_closed():
                    try:
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    except RuntimeError:
                        # Runner 可能恰在检查后完成关闭；只容忍这一竞态。
                        if not self._loop.is_closed():
                            raise
        if wait:
            self.wait_closed()

    def in_loop_thread(self) -> bool:
        return threading.current_thread() is self._thread

    @property
    def started(self) -> bool:
        return self._thread is not None

    def wait_closed(self) -> None:
        """等待已安排的关闭完成；不启动循环。"""
        if self.in_loop_thread():
            raise RuntimeError("cannot wait for LoopRunner from its own thread")
        thread = self._thread
        if thread is not None:
            thread.join()

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """提交协程到常驻 loop 并阻塞等待结果——可从任意线程调用。"""
        if self.in_loop_thread():
            coro.close()
            raise RuntimeError("cannot run synchronously on LoopRunner's own thread")
        try:
            with self._lock:
                future = asyncio.run_coroutine_threadsafe(coro, self.ensure_loop())
        except BaseException:
            coro.close()
            raise
        return future.result(timeout)
