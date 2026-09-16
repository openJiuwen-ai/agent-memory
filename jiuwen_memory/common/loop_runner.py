# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""进程级专职事件循环线程——任意线程提交协程到常驻 loop 执行。

解决"sync 调用方经 ``asyncio.run`` 建临时 loop，返回即关闭，绑上去的
后台 task 全部随 loop 消亡"的问题：协程提交到本 Runner 拥有的常驻
loop（daemon 线程 ``run_forever``），生命周期与进程一致，不再依赖
任何一次调用的临时 loop。

多个调用线程可并发提交（``run_coroutine_threadsafe`` 天然支持）。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


class LoopRunner:
    """进程级专职事件循环线程。

    ``run`` 可从任意线程调用：协程经 ``run_coroutine_threadsafe`` 提交到常驻
    loop，调用线程阻塞等待结果；``ensure_loop`` 取常驻 loop 供调用方自行
    ``run_coroutine_threadsafe`` 提交。多个调用线程可并发提交。
    线程为 daemon，随进程退出，不提供 ``close``——loop 本身不持有需释放的
    资源；调用方经本 Runner 跑的资源（如连接池）由调用方自行管理生命周期。
    """

    def __init__(self, thread_name: str = "agent-memory-loop") -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        """返回常驻 loop——不存在或线程已死时（惰性）重建。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._loop is None:
                    raise RuntimeError(
                        f"loop thread {self._thread_name!r} is alive but event loop is missing"
                    )
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name=self._thread_name, daemon=True
            )
            self._thread.start()
            return self._loop

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """提交协程到常驻 loop 并阻塞等待结果——可从任意线程调用。"""
        future = asyncio.run_coroutine_threadsafe(coro, self.ensure_loop())
        return future.result(timeout)
