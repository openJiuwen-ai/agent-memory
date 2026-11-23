# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""写入入口的同步步骤必须离开宿主事件循环（F10 决策 1）。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from jiuwen_memory.api import assemble
from jiuwen_memory.common.security.request_context import internal_context
from jiuwen_memory.common.type_def import Scope
from tests.support.scoped_authenticator import ScopedAuthenticator

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="alice")


def _security():
    return internal_context(ScopedAuthenticator(Scope(org="acme", space="", user="alice")))


async def _observe(coro, *, threshold: float, minimum: float) -> float:
    """跑 ``coro`` 的同时测事件循环最大停顿。"""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    try:
        await coro
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        await beat
    assert elapsed >= minimum, "测试前提：被观测步骤确实耗时"
    return max(gaps)


def test_slow_router_does_not_block_event_loop() -> None:
    api = assemble()
    original = api._write_target

    def slow_target(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    api._write_target = slow_target  # type: ignore[method-assign]
    try:
        gap = asyncio.run(
            _observe(
                api.add_async("慢路由", _SCOPE, security=_security()),
                threshold=0.15,
                minimum=0.3,
            )
        )
    finally:
        api.close()

    assert gap < 0.15, f"同步路由卡住事件循环：最大停顿 {gap:.3f}s"


def test_slow_ingestor_does_not_block_event_loop() -> None:
    api = assemble()
    original = api._engine._ingestor

    def slow_ingest(payloads):
        time.sleep(0.3)
        return original.ingest(payloads)

    api._engine._ingestor = SimpleNamespace(ingest=slow_ingest)
    try:
        gap = asyncio.run(
            _observe(
                api.add_async("慢摄入", _SCOPE, security=_security()),
                threshold=0.15,
                minimum=0.3,
            )
        )
    finally:
        api.close()

    assert gap < 0.15, f"同步摄入卡住事件循环：最大停顿 {gap:.3f}s"


def test_slow_router_does_not_block_batch_entry() -> None:
    api = assemble()
    original = api._batch_write_targets

    def slow_targets(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    api._batch_write_targets = slow_targets  # type: ignore[method-assign]
    try:
        gap = asyncio.run(
            _observe(
                api.batch_add_async(
                    [SimpleNamespace(content="a", scope=_SCOPE, source=None)],
                    _SCOPE,
                    security=_security(),
                ),
                threshold=0.15,
                minimum=0.3,
            )
        )
    finally:
        api.close()

    assert gap < 0.15, f"批量入口的同步路由卡住事件循环：最大停顿 {gap:.3f}s"
