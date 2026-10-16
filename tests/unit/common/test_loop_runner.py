"""LoopRunner 显式释放与线程池收尾。"""

import asyncio
import threading

import pytest

from jiuwen_memory.common.loop_runner import LoopRunner

pytestmark = pytest.mark.unit


def test_close_without_start_does_not_create_thread() -> None:
    runner = LoopRunner()
    runner.close()
    runner.close()
    assert not runner.started
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(asyncio.sleep(0))


def test_close_waits_for_default_executor_and_closes_loop() -> None:
    runner = LoopRunner()
    started, release = threading.Event(), threading.Event()

    def work() -> None:
        started.set()
        assert release.wait(10), "test must release executor work"

    async def launch() -> None:
        asyncio.create_task(asyncio.to_thread(work))

    try:
        runner.run(launch())
        assert started.wait(3)
        runner.close(wait=False)
        assert runner._thread is not None and runner._thread.is_alive()
    finally:
        release.set()
        runner.close()
    assert not runner._thread.is_alive()
    assert runner._loop is not None and runner._loop.is_closed()


def test_own_thread_cannot_block_waiting_for_itself() -> None:
    runner = LoopRunner()

    async def attempt() -> None:
        with pytest.raises(RuntimeError, match="own thread"):
            runner.close()
        with pytest.raises(RuntimeError, match="own thread"):
            runner.run(asyncio.sleep(0))

    try:
        runner.run(attempt(), timeout=3)
    finally:
        runner.close()
