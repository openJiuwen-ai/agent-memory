"""MiddleToLongJob 分布式锁接入测试。

覆盖决策 9 的关键行为：

- ``lock=None``（未配置）→ 直接走 ``_run_inner``，不取锁，行为与引入锁前一致；
- 取锁成功 → 临界区跑完并释放锁（evolver / lifecycle / index 被调用一次）；
- 取锁失败（``LockTimeoutError``）→ 跳过本次 tick，返回 ``SUCCEEDED + skipped_due_to_lock``，
  且**不调 evolver / storage**（避免无谓工作）；
- 同 task 重入 → ``reentrant=True``，正常返回，不起第二个续期 task。

测试用 ``InMemoryLockProvider``（进程内）验证互斥语义——它共用同一套
``LockProvider`` 模板逻辑，与 redis 实现行为一致。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.control.jobs_impl.middle_to_long_job import MiddleToLongJob
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.unit.control.test_middle_to_long_job import (
    _make_unit,
    _RecordingEvolver,
    _RecordingIndex,
    _RecordingLifecycle,
    _ScriptedLLM,
)

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="prod", user="u1")


def _build_job(
    scope: Scope,
    kv: InMemoryKVStore,
    *,
    lock: InMemoryLockProvider | None = None,
) -> tuple[MiddleToLongJob, _RecordingEvolver, _RecordingLifecycle, _RecordingIndex, _ScriptedLLM]:
    evolver = _RecordingEvolver()
    lifecycle = _RecordingLifecycle()
    index = _RecordingIndex()
    llm = _ScriptedLLM(['{"results":["true"]}'])
    job = MiddleToLongJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        lifecycle=lifecycle,
        index=index,
        llm=llm,
        concurrency=1,
        lock=lock,
    )
    return job, evolver, lifecycle, index, llm


def _seed_candidate(kv: InMemoryKVStore, scope: Scope, uid: str = "w1") -> None:
    unit = _make_unit(uid, scope, "hi")
    kv.insert(scope, memory_key(uid), dumps(unit))


# ---- lock=None：行为不变 ----


def test_lock_none_runs_inner_directly() -> None:
    """未注入 LockProvider 时 run() 直接走 _run_inner，不取锁，临界区正常执行。"""
    kv = InMemoryKVStore()
    _seed_candidate(kv, _SCOPE)
    job, evolver, lifecycle, index, _ = _build_job(_SCOPE, kv, lock=None)

    result = asyncio.run(job.run())

    assert result.status == JobStatus.SUCCEEDED
    assert "skipped_due_to_lock" not in result.detail
    assert evolver.calls, "evolver should be called when no lock"
    assert lifecycle.transition_calls, "lifecycle.transition should be called"
    assert index.removed, "index.remove should be called"


# ---- 取锁成功 ----


def test_lock_acquired_runs_critical_section_and_releases() -> None:
    """注入 LockProvider 时取锁成功，临界区执行，锁在 run 结束后释放（他人可重新获取）。"""
    kv = InMemoryKVStore()
    _seed_candidate(kv, _SCOPE)
    lock = InMemoryLockProvider(lease_ms=30_000, wait_timeout_ms=0)
    job, evolver, _, _, _ = _build_job(_SCOPE, kv, lock=lock)

    async def scenario() -> None:
        result = await job.run()
        assert result.status == JobStatus.SUCCEEDED
        assert "skipped_due_to_lock" not in result.detail
        assert evolver.calls, "critical section must run when lock acquired"
        # 锁应已释放——他人现在能立即取到
        fresh = await asyncio.create_task(
            lock.acquire(_SCOPE, "middle_to_long", wait_timeout_ms=0)
        )
        await lock.release(fresh)

    asyncio.run(scenario())


# ---- 取锁失败：跳过本次 tick ----


def test_lock_timeout_skips_tick_without_calling_evolver() -> None:
    """锁被他人持有，本次 run 取锁失败 → 跳过 tick，不调 evolver / lifecycle / index。

    scheduler 下个 tick 会继续重试（候选仍在 KV，不会丢失）。
    """
    kv = InMemoryKVStore()
    _seed_candidate(kv, _SCOPE)
    lock = InMemoryLockProvider(lease_ms=30_000, wait_timeout_ms=0)
    job, evolver, lifecycle, index, _ = _build_job(_SCOPE, kv, lock=lock)

    async def scenario() -> None:
        # 在另一 task 占住锁——同 task 会被判为重入，必须 create_task 派生
        holder = await asyncio.create_task(
            lock.acquire(_SCOPE, "middle_to_long", wait_timeout_ms=0)
        )
        try:
            result = await job.run()
            assert result.status == JobStatus.SUCCEEDED
            assert result.detail.get("skipped_due_to_lock") == "true"
            assert not evolver.calls, "evolver must NOT be called when lock unavailable"
            assert not lifecycle.transition_calls, "lifecycle must NOT be called"
            assert not index.removed, "index must NOT be called"
        finally:
            await lock.release(holder)

    asyncio.run(scenario())


# ---- 重入：同 task 嵌套 ----


def test_reentry_within_same_task_does_not_block() -> None:
    """同 task 内嵌套 acquire 同一锁 → reentrant=True，正常返回，不起第二个续期 task。"""
    lock = InMemoryLockProvider(lease_ms=30_000, wait_timeout_ms=0)
    kv = InMemoryKVStore()
    _seed_candidate(kv, _SCOPE)
    job, _, _, _, _ = _build_job(_SCOPE, kv, lock=lock)

    async def scenario() -> None:
        before_tasks = len(asyncio.all_tasks())
        async with lock.guard(_SCOPE, "middle_to_long"):
            # 此时已持有锁；run() 内的 guard 应判为重入，不再起第二个续期 task
            result = await job.run()
            assert result.status == JobStatus.SUCCEEDED
            assert "skipped_due_to_lock" not in result.detail
        # 重入不增加 task 数（guard 检测 reentrant 后跳过 renewer）
        after_tasks = len(asyncio.all_tasks())
        assert after_tasks <= before_tasks + 1  # 至多多 1 个（外层 guard 的续期 task）

    asyncio.run(scenario())
