"""common.security.protection.workload_guard: 昂贵操作的并发预算。"""

from __future__ import annotations

import threading

import pytest

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.security.protection.protection_impl.semaphore_guard import (
    SemaphoreWorkloadGuard,
)
from jiuwen_memory.common.security.protection.workload_guard import WorkloadGuardProducer
from jiuwen_memory.config.context import AssemblyContext

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True, scope="module")
def _registered():
    register_plugins()


# -- 预算语义 ---------------------------------------------------------------- #


def test_budget_is_exhausted_after_max_concurrent_acquires() -> None:
    guard = SemaphoreWorkloadGuard(2)
    assert guard.acquire() is True
    assert guard.acquire() is True
    assert guard.acquire() is False


def test_release_returns_the_slot() -> None:
    guard = SemaphoreWorkloadGuard(1)
    assert guard.acquire() is True
    assert guard.acquire() is False
    guard.release()
    assert guard.acquire() is True


def test_acquire_never_blocks() -> None:
    """耗尽即快速拒绝，不排队——排队把资源耗尽从 CPU/内存转移到线程与请求队列。

    若实现改成阻塞式 acquire，本测试会挂在 join 上直到超时。
    """
    guard = SemaphoreWorkloadGuard(1)
    assert guard.acquire() is True

    result: list[bool] = []
    done = threading.Event()

    def worker() -> None:
        result.append(guard.acquire())
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert done.wait(timeout=2.0), "acquire 阻塞了——预算耗尽必须立即返回 False"
    thread.join()
    assert result == [False]


def test_over_release_does_not_inflate_the_budget() -> None:
    """acquire 失败后误 release 不能凭空造出槽位，否则预算上限被悄悄突破。"""
    guard = SemaphoreWorkloadGuard(1)
    guard.release()  # 越界 release：记录但不抛
    assert guard.acquire() is True
    assert guard.acquire() is False


def test_concurrent_acquires_do_not_exceed_budget() -> None:
    """20 个线程同时抢 5 个槽位：成功数必须恰好等于预算。"""
    guard = SemaphoreWorkloadGuard(5)
    acquired: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def hammer() -> None:
        barrier.wait()
        got = guard.acquire()
        with lock:
            acquired.append(got)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(acquired) == 5


def test_max_concurrent_is_exposed_for_diagnostics() -> None:
    assert SemaphoreWorkloadGuard(7).max_concurrent == 7


def test_process_local_guard_does_not_claim_distributed_budget() -> None:
    """多副本部署下 N 个副本 = N 倍实际并发：能力由类型声明，不按 target 名推断。"""
    assert SemaphoreWorkloadGuard(1).supports_distributed_budget() is False


# -- 装配 -------------------------------------------------------------------- #


def test_build_uses_defaults() -> None:
    guard = WorkloadGuardProducer.build("semaphore", {}, AssemblyContext())
    assert isinstance(guard, SemaphoreWorkloadGuard)
    assert guard.max_concurrent >= 1
    guard.health()


def test_build_honours_params() -> None:
    guard = WorkloadGuardProducer.build("semaphore", {"max_concurrent": 2}, AssemblyContext())
    assert guard.max_concurrent == 2


@pytest.mark.parametrize("max_concurrent", [0, -1])
def test_invalid_budget_rejected_at_assembly(max_concurrent) -> None:
    """预算为 0 会拒绝一切昂贵操作（认证全挂），必须在启动期炸而非运行期。"""
    with pytest.raises(ValidationError):
        WorkloadGuardProducer.build(
            "semaphore", {"max_concurrent": max_concurrent}, AssemblyContext()
        )


def test_named_instances_are_shared() -> None:
    """进程级共享通过具名实例表达，不用模块级单例（见 Producer docstring）。"""
    ctx = AssemblyContext.from_dict(
        {"workload_guard": {"default": {"target": "semaphore", "params": {"max_concurrent": 1}}}}
    )
    first = WorkloadGuardProducer.build_named("default", ctx)
    second = WorkloadGuardProducer.build_named("default", ctx)
    assert first is second

    # 真正共享预算：一个持有者占满后另一个引用也拿不到槽位。
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
