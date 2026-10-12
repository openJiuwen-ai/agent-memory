# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Owned loop threads release tasks, executors and the loop on shutdown."""

import asyncio
import inspect
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from jiuwen_memory.common.loop_runner import LoopRunner

pytestmark = pytest.mark.unit


def test_close_unused_runner_does_not_start_a_thread() -> None:
    runner = LoopRunner()
    runner.close()
    runner.close()
    assert runner._thread is None
    with pytest.raises(RuntimeError, match="closed"):
        runner.ensure_loop()


def test_close_cancels_pending_tasks_and_closes_loop() -> None:
    runner = LoopRunner()
    started, finished = threading.Event(), threading.Event()

    async def pending():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            finished.set()

    future = runner.submit(pending())
    assert started.wait(3)
    runner.close()
    assert finished.is_set()
    assert not runner._thread.is_alive()
    assert runner._loop.is_closed()
    with pytest.raises(CancelledError):
        future.result(timeout=3)
    coro = pending()
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(coro)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_close_waits_for_default_executor() -> None:
    runner = LoopRunner()
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def work():
        started.set()
        assert release.wait(3)

    runner.submit(asyncio.to_thread(work))
    assert started.wait(3)
    with ThreadPoolExecutor() as callers:
        future = callers.submit(lambda: (runner.close(), closed.set()))
        try:
            assert not closed.wait(0.05)
        finally:
            release.set()
        future.result(timeout=3)
    assert not runner._thread.is_alive()


def test_blocking_operations_on_owned_thread_fail_without_deadlock() -> None:
    runner = LoopRunner()

    async def from_loop():
        with pytest.raises(RuntimeError, match="own thread"):
            runner.run(asyncio.sleep(0))
        with pytest.raises(RuntimeError, match="own thread"):
            runner.close()

    try:
        runner.run(from_loop(), timeout=3)
    finally:
        runner.close()


def test_concurrent_submit_and_close_resolve_all_accepted_futures() -> None:
    runner = LoopRunner()
    gate = threading.Barrier(9)

    def submit():
        gate.wait(timeout=3)
        try:
            return runner.submit(asyncio.sleep(0, result=42))
        except RuntimeError as exc:
            assert "closed" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=8) as callers:
        attempts = [callers.submit(submit) for _ in range(8)]
        gate.wait(timeout=3)
        runner.close()
        for attempt in attempts:
            accepted = attempt.result(timeout=3)
            if accepted is not None:
                try:
                    assert accepted.result(timeout=3) == 42
                except CancelledError:
                    pass
