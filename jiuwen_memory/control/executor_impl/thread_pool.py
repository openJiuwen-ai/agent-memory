# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``thread_pool`` 执行器：llm / io 双线程池 + 重入矩阵（F10 决策 1）。"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory

from ..base import ControlOperatorType
from ..executor import BlockingExecutor, BlockingExecutorProducer, ExecutorLane

T = TypeVar("T")

DEFAULT_LLM_WORKERS = 8
DEFAULT_IO_WORKERS = min(32, (os.cpu_count() or 1) + 4)


class ThreadPoolBlockingExecutor(BlockingExecutor):
    """两条通道各一个线程池。

    线程名带 ``am-llm`` / ``am-io`` 前缀，便于线上定位「谁占着线程」。线程数只是资源
    上限，**不承担 LLM 限流职责**（F10 非目标）。
    """

    def __init__(
        self,
        *,
        llm_workers: int = DEFAULT_LLM_WORKERS,
        io_workers: int = DEFAULT_IO_WORKERS,
    ) -> None:
        for name, value in (("llm_workers", llm_workers), ("io_workers", io_workers)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"executor {name} must be a positive int")
        self._lane_local = threading.local()
        self._close_lock = threading.Lock()
        self._closed = False
        self._pools = {
            lane: ThreadPoolExecutor(
                max_workers=llm_workers if lane is ExecutorLane.LLM else io_workers,
                thread_name_prefix=f"am-{lane.value}",
                initializer=self._bind_worker,
                initargs=(lane,),
            )
            for lane in ExecutorLane
        }

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.EXECUTOR

    def health(self) -> None:
        if self._closed:
            raise RuntimeError("executor closed")

    def current_lane(self) -> ExecutorLane | None:
        lane = getattr(self._lane_local, "lane", None)
        return lane if isinstance(lane, ExecutorLane) else None

    async def run(self, lane: ExecutorLane, fn: Callable[..., T], /, *args, **kwargs) -> T:
        if not isinstance(lane, ExecutorLane):
            raise ValidationError("executor lane must be ExecutorLane")
        self._ensure_open()
        call = functools.partial(fn, *args, **kwargs)
        context = contextvars.copy_context()
        current = self.current_lane()
        if current is ExecutorLane.LLM or current is lane:
            # 重入矩阵：llm 工作线程一律内联；io 工作线程只内联 io。
            return context.run(call)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pools[lane], context.run, call)

    def close(self, *, wait: bool = True) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            pools = list(self._pools.values())
        if wait and self.current_lane() is not None:
            # 从工作线程里 close 不能 join 自己所在的池，否则永久等待。
            wait = False
        for pool in pools:
            pool.shutdown(wait=wait)

    def _ensure_open(self) -> None:
        if not self._closed:
            return
        raise RuntimeError("executor closed")

    def _bind_worker(self, lane: ExecutorLane) -> None:
        self._lane_local.lane = lane


@BlockingExecutorProducer.register("thread_pool")
def _build(config: Any) -> ThreadPoolBlockingExecutor:
    return ThreadPoolBlockingExecutor(
        llm_workers=int(Factory.cfg_get(config, "llm_workers", DEFAULT_LLM_WORKERS)),
        io_workers=int(Factory.cfg_get(config, "io_workers", DEFAULT_IO_WORKERS)),
    )
