"""LoopRunner 显式释放与线程池收尾。"""

# pylint: disable=protected-access  # 白盒验证线程退出和循环关闭，不为测试扩展生产接口

import asyncio
import threading
from types import SimpleNamespace

import pytest

from jiuwen_memory.common.loop_runner import LoopRunner

pytestmark = pytest.mark.unit


def test_close_after_worker_has_already_exited() -> None:
    runner = LoopRunner()
    loop = runner.ensure_loop()
    worker = runner._thread
    try:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        assert not worker.is_alive() and loop.is_closed(), "simulate an out-of-band loop exit"
        runner.close()
        runner.close()
        assert not worker.is_alive(), "close must not restart an already exited worker"
    finally:
        runner.close()


@pytest.mark.parametrize("closed_before_check", [True, False])
def test_close_waits_even_if_loop_is_closed_before_or_during_stop(
    closed_before_check: bool,
) -> None:
    calls: list[str] = []
    state = {"closed": closed_before_check}

    def stop(callback) -> None:
        calls.append("stop")
        state["closed"] = True
        raise RuntimeError("Event loop is closed")

    runner = LoopRunner()
    runner._loop = SimpleNamespace(
        is_closed=lambda: state["closed"], stop=lambda: None, call_soon_threadsafe=stop
    )
    runner._thread = SimpleNamespace(join=lambda: calls.append("join"))
    runner.close()
    expected = ["join"] if closed_before_check else ["stop", "join"]
    assert calls == expected, "closed-loop handling must still wait for the worker"


def test_close_does_not_hide_other_runtime_errors() -> None:
    def stop(callback) -> None:
        raise RuntimeError("unexpected stop failure")

    runner = LoopRunner()
    runner._loop = SimpleNamespace(
        is_closed=lambda: False, stop=lambda: None, call_soon_threadsafe=stop
    )
    with pytest.raises(RuntimeError, match="unexpected stop failure"):
        runner.close()


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


def test_loop_is_created_in_its_worker_thread(monkeypatch) -> None:
    created_in: list[threading.Thread] = []
    original = asyncio.events.new_event_loop

    def create_loop():
        created_in.append(threading.current_thread())
        return original()

    monkeypatch.setattr(asyncio.events, "new_event_loop", create_loop)
    runner = LoopRunner()
    try:
        runner.run(asyncio.sleep(0), timeout=3)
        assert created_in == [runner._thread], "loop must be owned by its worker from creation"
    finally:
        runner.close()


def test_loop_start_failure_reaches_caller_and_does_not_hang(monkeypatch) -> None:
    def fail():
        raise OSError("loop creation failed")

    monkeypatch.setattr(asyncio.events, "new_event_loop", fail)
    runner = LoopRunner()
    try:
        with pytest.raises(RuntimeError, match="failed to start") as caught:
            runner.run(asyncio.sleep(0), timeout=3)
        assert isinstance(caught.value.__cause__, OSError), "preserve the startup failure cause"
        assert not runner._thread.is_alive(), "failed startup must not leave a thread behind"
    finally:
        runner.close()


def test_close_finalizes_pending_tasks_and_async_generators() -> None:
    runner = LoopRunner()
    cancelled, finalized = threading.Event(), threading.Event()

    async def stream():
        try:
            yield 1
        finally:
            finalized.set()

    async def pending() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def launch():
        generator = stream()
        await anext(generator)
        task = asyncio.create_task(pending())
        await asyncio.sleep(0)
        return generator, task

    try:
        generator, task = runner.run(launch(), timeout=3)
    finally:
        runner.close()
    assert cancelled.is_set() and task.cancelled(), "Runner must cancel remaining tasks"
    assert finalized.is_set(), "Runner must finalize open asynchronous generators"
    assert generator.ag_frame is None, "generator cleanup must complete before thread exits"
