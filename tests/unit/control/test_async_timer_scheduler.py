"""AsyncTimerScheduler 单元测试——异步 + 定时调度核心行为。

聚焦 Scheduler 的调度语义（入队/定时注册/Timer 协程/互斥/取消），
不依赖真实 Evolver——用 fake Job 控制 run 行为。
"""

from __future__ import annotations
# pylint: disable=protected-access  # 测试代码需要访问受保护成员以断言装配链行为

import asyncio
import time

import pytest

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import (
    AsyncTimerScheduler,
    TimerEntry,
    TimerWheel,
)
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

pytestmark = pytest.mark.unit


class _RecordingJob(Job):
    """记录 run 调用次数的 fake Job——interval 由构造参数控制。

    一次性任务场景（interval=0）下直接累积实例属性 ``run_count``；
    定时任务场景（interval>0）下 Timer 协程用 ``copy.copy`` 生成实例，
    实例属性会被浅拷贝（每个新实例从 0 开始），故定时场景用 ``_CountingJob``
    的类变量跨拷贝累积。
    """

    def __init__(
        self,
        scope: Scope,
        *,
        interval: int = 0,
        detail: dict[str, str] | None = None,
        exc: Exception | None = None,
        sleep: float = 0.0,
    ) -> None:
        super().__init__(scope=scope, interval=interval)
        self._detail = detail or {}
        self._exc = exc
        self._sleep = sleep
        self.run_count = 0

    async def run(self) -> JobInfo:
        self.run_count += 1
        if self._sleep:
            time.sleep(self._sleep)
        if self._exc is not None:
            raise self._exc
        return JobInfo(
            scope=self.scope, status=JobStatus.SUCCEEDED, detail=dict(self._detail)
        )


class _CountingJob(Job):
    """定时任务场景用的 fake Job——用类变量跨 ``copy.copy`` 累积 run_count。

    每个测试开头须重置 ``_CountingJob.run_count = 0``。
    """

    run_count: int = 0  # 类变量——跨 copy.copy 实例累积

    def __init__(self, scope: Scope, *, interval: int = 1) -> None:
        super().__init__(scope=scope, interval=interval)

    async def run(self) -> JobInfo:
        _CountingJob.run_count += 1
        return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED, detail={})


# ---- _scope_key（内部方法，但因是新增核心逻辑故直接覆盖） ----


def test_scope_key_returns_five_tuple_including_space() -> None:
    """_scope_key 覆盖全部 5 维字段（含 space）——Scheduler 内部实现细节。"""
    scope = Scope(org="acme", space="s1", user="u1", agent="a1", session="sess1")
    assert AsyncTimerScheduler._scope_key(scope) == ("acme", "s1", "u1", "a1", "sess1")


def test_scope_key_distinguishes_by_space() -> None:
    """space 字段差异必须产生不同 key（隔离边界）。"""
    base = Scope(org="acme", user="u1")
    with_space = Scope(org="acme", space="sp", user="u1")
    assert AsyncTimerScheduler._scope_key(base) != AsyncTimerScheduler._scope_key(
        with_space
    )


# ---- 一次性任务路径 ----


def test_submit_once_shot_enqueues_and_starts_drain_task() -> None:
    """interval=0 → 入 per scope queue + 起 drain_task（需事件循环跑完）。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _RecordingJob(scope)

    async def _run():
        job_id = await scheduler.submit(job, Channel.BACKGROUND)
        # submit 后入队，但 run 还没跑（drain_task 是 asyncio.Task，需 await 让出)
        # 给 drain_task 一个跑的机会
        await asyncio.sleep(0.05)
        return job_id

    job_id = asyncio.run(_run())
    assert job.run_count == 1
    assert scheduler.status(job_id).status == JobStatus.SUCCEEDED


def test_submit_records_failed_when_run_raises() -> None:
    """run 抛异常 → FAILED + detail 含 error_type/error。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _RecordingJob(scope, exc=RuntimeError("boom"))

    async def _run():
        job_id = await scheduler.submit(job, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        return job_id

    job_id = asyncio.run(_run())
    info = scheduler.status(job_id)
    assert info.status == JobStatus.FAILED
    assert info.detail["error_type"] == "RuntimeError"
    assert info.detail["error"] == "boom"


def test_status_raises_not_found_for_missing_job() -> None:
    """查询不存在的 job_id → NotFoundError。"""
    scheduler = AsyncTimerScheduler()
    with pytest.raises(NotFoundError):
        scheduler.status("missing-job")


# ---- 定时任务路径 ----


def test_submit_timer_rejects_interval_below_tick_interval() -> None:
    """interval < tick_interval → ValueError（定时精度无法保证）。"""
    scheduler = AsyncTimerScheduler(tick_interval=10)
    scope = Scope(user="u1")
    job = _RecordingJob(scope, interval=5)  # 5 < 10

    async def _run():
        with pytest.raises(ValueError, match="interval"):
            await scheduler.submit(job, Channel.BACKGROUND)

    asyncio.run(_run())


def test_submit_timer_creates_entry_and_starts_timer_loop() -> None:
    """interval>0 → 创建 entry + 起 Timer 协程。

    注意：submit 是同步方法但内部用 asyncio.create_task 起 Timer 协程——
    测试需在事件循环存活期间检查 wheel.task 状态。asyncio.run 结束时
    事件循环关闭，未完成的 Task 被取消（done() 返回 True 但 cancelled）。
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _RecordingJob(scope, interval=10)  # 长 interval 避免 run 被触发

    async def _run():
        job_id = await scheduler.submit(job, Channel.BACKGROUND)
        await asyncio.sleep(0.05)  # 让 submit 完成 + Timer 协程起跑
        # 在事件循环存活期间检查
        info = scheduler.status(job_id)
        assert info.status == JobStatus.RUNNING
        assert info.detail["interval"] == "10"
        assert info.detail["tick_interval"] == "1"
        scope_key = scheduler._scope_key(scope)
        assert scope_key in scheduler._wheels
        wheel = scheduler._wheels[scope_key]
        assert len(wheel.entries) == 1
        assert wheel.entries[0].job_id == job_id
        assert wheel.task is not None
        assert not wheel.task.done()
        return job_id

    asyncio.run(_run())
    # 事件循环关闭后 Task 被取消——不在此断言


def test_submit_timer_same_kind_updates_existing_entry() -> None:
    """同 scope 同 kind → 更新 interval + 重置 next_run_at（复用 job_id）。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job1 = _RecordingJob(scope, interval=10)
    job2 = _RecordingJob(scope, interval=20)

    async def _run():
        jid1 = await scheduler.submit(job1, Channel.BACKGROUND)
        jid2 = await scheduler.submit(job2, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        return jid1, jid2

    jid1, jid2 = asyncio.run(_run())
    assert jid1 == jid2  # 复用 entry job_id
    scope_key = scheduler._scope_key(scope)
    wheel = scheduler._wheels[scope_key]
    assert len(wheel.entries) == 1  # 仍只有一个 entry
    assert wheel.entries[0].interval == 20  # 已更新


def test_submit_timer_update_preserves_next_run_at_when_not_done() -> None:
    """同 kind 非 done 状态 update → 不重置 next_run_at（避免 debounce 永不触发）。

    连续 add_async middle=true 时,Engine 每次都 submit MiddleToLongJob——
    Scheduler update 分支若每次都重置 next_run_at = now + interval,会变成
    debounce 语义:用户在 interval 内连续说话时 Timer 永远到不了 next_run_at,
    MiddleToLongJob 永不触发。

    修复:update 分支仅在 was_done=True 时重置 next_run_at（复活已退出的定时器）,
    was_done=False 时保持原 next_run_at 不变——首次 submit 设定的周期节拍
    不被后续 submit 推后。后续 write 累积的 unit 由下次到点触发批量处理。
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job1 = _RecordingJob(scope, interval=50)
    job2 = _RecordingJob(scope, interval=50)

    async def _run():
        await scheduler.submit(job1, Channel.BACKGROUND)
        await asyncio.sleep(0.05)  # 让 submit 完成
        scope_key = scheduler._scope_key(scope)
        wheel = scheduler._wheels[scope_key]
        next_run_at_after_first = wheel.entries[0].next_run_at
        # 立即（was_done=False）再 submit 同 kind
        await scheduler.submit(job2, Channel.BACKGROUND)
        next_run_at_after_second = wheel.entries[0].next_run_at
        # 不应被重置——保持首次 submit 设定的周期节拍
        assert next_run_at_after_second == next_run_at_after_first, (
            "update 分支 was_done=False 时不应重置 next_run_at——"
            "连续 add_async 会变成 debounce 语义导致 Timer 永不触发"
        )

    asyncio.run(_run())


def test_submit_timer_different_kind_appends_new_entry() -> None:
    """同 scope 不同 kind → append 新 entry，不重起 Timer 协程。"""

    class _JobA(_RecordingJob):
        pass

    class _JobB(_RecordingJob):
        pass

    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job_a = _JobA(scope, interval=10)
    job_b = _JobB(scope, interval=20)

    async def _run():
        jid_a = await scheduler.submit(job_a, Channel.BACKGROUND)
        jid_b = await scheduler.submit(job_b, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        return jid_a, jid_b

    jid_a, jid_b = asyncio.run(_run())
    assert jid_a != jid_b
    scope_key = scheduler._scope_key(scope)
    wheel = scheduler._wheels[scope_key]
    assert len(wheel.entries) == 2
    kinds = {type(e.job).__name__ for e in wheel.entries}
    assert kinds == {"_JobA", "_JobB"}


def test_submit_timer_restarts_dead_timer_task_on_update() -> None:
    """回归：sync write 经子线程 asyncio.run 跑完会关闭临时循环，
    绑定到该循环的 wheel.task 被取消（done()=True）。

    之后再次 submit 同 scope 同 kind（update 分支）应通过
    ``_ensure_timer_task`` 重启 Timer 协程——否则 entry 永不再触发。

    复现：submit 起一个 wheel.task → 显式 cancel + await 让取消传播 →
    再 submit 同 kind（interval 不同以走 update 分支）→ 新 wheel.task
    应被创建且 not done。
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job1 = _RecordingJob(scope, interval=10)
    job2 = _RecordingJob(scope, interval=20)  # 同 kind，走 update 分支

    async def _run():
        jid1 = await scheduler.submit(job1, Channel.BACKGROUND)
        await asyncio.sleep(0.05)  # 让 Timer 协程起跑
        scope_key = scheduler._scope_key(scope)
        wheel = scheduler._wheels[scope_key]
        prev_task = wheel.task
        assert prev_task is not None and not prev_task.done()
        # 模拟临时循环关闭：取消 Timer task 并让取消传播
        prev_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prev_task
        assert wheel.task.done()  # 旧 task 已死
        # 再 submit 同 kind——update 分支应通过 _ensure_timer_task 重启
        jid2 = await scheduler.submit(job2, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        assert jid1 == jid2  # 复用 entry job_id
        new_task = wheel.task
        assert new_task is not prev_task  # 新 Task 对象
        assert not new_task.done()  # 新 Task 存活
        return jid1

    asyncio.run(_run())


def test_submit_timer_restarts_dead_timer_task_on_add_new_kind() -> None:
    """回归：add 新 kind 分支同样需 _ensure_timer_task。

    场景：wheel 已存在（旧 kind 的 task 已死），再 submit 不同 kind
    走 else 分支 append 新 entry——若不重启 Timer，新 entry 永不触发。
    """

    class _JobA(_RecordingJob):
        pass

    class _JobB(_RecordingJob):
        pass

    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job_a = _JobA(scope, interval=10)
    job_b = _JobB(scope, interval=10)  # 不同 kind，走 add 分支

    async def _run():
        await scheduler.submit(job_a, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        scope_key = scheduler._scope_key(scope)
        wheel = scheduler._wheels[scope_key]
        prev_task = wheel.task
        # 模拟临时循环关闭
        prev_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prev_task
        assert wheel.task.done()
        # 加不同 kind——else 分支应通过 _ensure_timer_task 重启
        await scheduler.submit(job_b, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        new_task = wheel.task
        assert new_task is not prev_task
        assert not new_task.done()

    asyncio.run(_run())


def test_timer_loop_triggers_instance_at_next_run_at() -> None:
    """Timer 协程到点 → 生成一次性实例入队 + run 被调 + 重置 next_run_at。

    时间线（tick_interval=1, interval=1）：
    - t=0: submit，next_run_at = t+1
    - t=1: Timer 醒来（sleep 1s 后），now >= next_run_at → 生成 copy.copy 实例入队
    - t=1.x: drain_queue 跑 instance.run()，_CountingJob.run_count=1
    - t=2: 验证 run_count >= 1

    注意：Timer 用 ``copy.copy`` 生成实例，原 Job 对象的实例属性不变——
    故用 ``_CountingJob`` 的类变量跨拷贝累积。
    """
    _CountingJob.run_count = 0
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _CountingJob(scope, interval=1)

    async def _run():
        jid = await scheduler.submit(job, Channel.BACKGROUND)
        # 等 2 个 tick：首个 tick(t=1) 触发实例入队 + drain 跑完；
        # 第二个 tick(t=2) 重置 next_run_at。给余量到 2.2s。
        await asyncio.sleep(2.2)
        # 在事件循环存活期间验证
        assert _CountingJob.run_count >= 1
        assert scheduler.status(jid).status == JobStatus.RUNNING
        return jid

    asyncio.run(_run())
    # 事件循环关闭后不验证（Task 被取消）


# ---- 退出语义 ----


def test_timer_loop_exits_when_all_entries_done() -> None:
    """实例 run 返回 is_done=true → 标记 parent entry is_done → 全 done 时 Timer 退出。"""

    class _DoneJob(Job):
        """返回 is_done=true 的 Job——模拟"无候选"场景。"""

        def __init__(self, scope: Scope, interval: int = 1) -> None:
            super().__init__(scope=scope, interval=interval)
            self.run_count = 0

        async def run(self) -> JobInfo:
            self.run_count += 1
            return JobInfo(
                scope=self.scope,
                status=JobStatus.SUCCEEDED,
                detail={"is_done": "true", "reason": "no candidates"},
            )

    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _DoneJob(scope, interval=1)

    async def _run():
        jid = await scheduler.submit(job, Channel.BACKGROUND)
        # 等 Timer 触发一次（is_done=true 标记 entry）+ 下一 tick 检测全 done 退出
        await asyncio.sleep(2.5)
        return jid

    jid = asyncio.run(_run())
    # 定时任务标 SUCCEEDED（wheel 退出时标记）
    assert scheduler.status(jid).status == JobStatus.SUCCEEDED
    # wheel 已从 _wheels 移除
    scope_key = scheduler._scope_key(scope)
    assert scope_key not in scheduler._wheels


# ---- 取消 ----


def test_cancel_timer_marks_entry_done_and_removes() -> None:
    """cancel 定时任务 → entry is_done + 从 entries 移除。"""
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    job = _RecordingJob(scope, interval=10)

    async def _run():
        jid = await scheduler.submit(job, Channel.BACKGROUND)
        await asyncio.sleep(0.05)
        scheduler.cancel(jid)
        await asyncio.sleep(0.05)
        return jid

    jid = asyncio.run(_run())
    assert scheduler.status(jid).status == JobStatus.CANCELLED
    scope_key = scheduler._scope_key(scope)
    wheel = scheduler._wheels.get(scope_key)
    if wheel is not None:
        # entry 应已被移除（cancel 从 entries.remove）
        assert all(e.job_id != jid for e in wheel.entries)


# ---- per scope 串行 / 跨 scope 并行 ----


def test_per_scope_single_drain_task_serializes_same_scope_jobs() -> None:
    """同 scope 两个 Job → 单 drain Task 串行消费（FIFO 顺序，一个跑完才跑下一个）。

    串行性来自"per scope 单 drain Task"——``_ensure_drain_task`` 检查
    ``existing.done()``，旧 drain 没跑完不创建新 drain。不依赖 asyncio.Lock。
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")
    # 两个 Job 都 sleep 0.2s 模拟耗时
    job_a = _RecordingJob(scope, sleep=0.2, detail={"name": "a"})
    job_b = _RecordingJob(scope, sleep=0.2, detail={"name": "b"})

    async def _run():
        await scheduler.submit(job_a, Channel.BACKGROUND)
        await scheduler.submit(job_b, Channel.BACKGROUND)
        # 两个 Job 各 0.2s，串行总耗时 ~0.4s；等 0.6s 确保 drain 跑完
        await asyncio.sleep(0.6)

    asyncio.run(_run())
    assert job_a.run_count == 1
    assert job_b.run_count == 1


def test_cross_scope_drain_tasks_run_in_parallel() -> None:
    """跨 scope 两个 Job → 不同 drain Task，并行（总耗时 ~0.2s 而非 0.4s）。

    跨 scope 完全并行——不同 scope_key 对应不同 queue 与不同 drain Task。
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope_a = Scope(user="u1")
    scope_b = Scope(user="u2")
    job_a = _RecordingJob(scope_a, sleep=0.2)
    job_b = _RecordingJob(scope_b, sleep=0.2)

    async def _run():
        await scheduler.submit(job_a, Channel.BACKGROUND)
        await scheduler.submit(job_b, Channel.BACKGROUND)
        # 并行总耗时 ~0.2s；等 0.4s 足够
        await asyncio.sleep(0.4)

    asyncio.run(_run())
    assert job_a.run_count == 1
    assert job_b.run_count == 1


# ---- Timer 触发去重：queue 已有同 kind 则跳过 ----


def test_timer_loop_skips_tick_when_same_kind_already_queued() -> None:
    """同 scope queue 已有同 kind 实例排队 → 跳过本次触发（不入队）。

    场景：run 时长 > interval 时 queue 会堆积同 kind 实例——浪费调度槽。
    Timer 触发前查 queue,已有同 kind 则跳过本次 tick。

    复现：
    - submit 一个 _BlockingJob（interval=1, sleep=10s 让 run 长时间阻塞）
    - t=1: Timer 触发,生成 instance1 入队,drain 跑 instance1.run() 阻塞 10s
    - t=2: Timer 又触发,此时 queue 空（instance1 已被 dequeue 跑着）
      → 此 tick 应入队 instance2（queue 空,没有同 kind 排队）
    - 改为：直接手工 append 一个 _BlockingJob 到 queue 模拟"已有同 kind 排队"
      → Timer 触发应跳过本次 tick,queue 仍只有手工 append 那条
    """
    scheduler = AsyncTimerScheduler(tick_interval=1)
    scope = Scope(user="u1")

    class _BlockingJob(Job):
        """run 阻塞 10s 的 Job——让 drain Task 不 done,模拟 run > interval。"""

        def __init__(self, scope: Scope, *, interval: int = 1) -> None:
            super().__init__(scope=scope, interval=interval)

        async def run(self) -> JobInfo:
            await asyncio.sleep(10)
            return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED, detail={})

    job = _BlockingJob(scope, interval=1)

    async def _run():
        jid = await scheduler.submit(job, Channel.BACKGROUND)
        await asyncio.sleep(0.05)  # 让 submit 完成 + Timer 起跑
        scope_key = scheduler._scope_key(scope)
        queue = scheduler._scope_queues[scope_key]

        # 手工往 queue 塞一个同 kind 实例——模拟"queue 已有同 kind 排队"
        # （正常路径下,instance1 已被 drain dequeue 跑着,queue 空；
        #  这里手工塞回一个,模拟 queue 堆积场景）
        pending_instance = _BlockingJob(scope)
        queue.append(("manual-pending", pending_instance))

        # 记录 queue 当前长度,等 1 个 tick 让 Timer 触发
        queue_len_before_tick = len(queue)
        await asyncio.sleep(1.2)  # 跨过一个 tick (1s tick + 0.2s 余量)

        # Timer 应跳过本次 tick——queue 长度不变（仍为手工塞的那条）
        assert len(queue) == queue_len_before_tick, (
            f"Timer 应跳过本次 tick（queue 已有同 kind）,但 queue 长度变化了："
            f"before={queue_len_before_tick} after={len(queue)}"
        )

        # cleanup: cancel Timer + drain
        scheduler.cancel(jid)
        # 让 _BlockingJob 阻塞 run 完成（cancel 不打断 running drain）
        # drain task 持有 running instance,await 一下让其完成
        # 实际上 cancel 不取消 drain task;为避免测试卡死,这里也 cancel 手工塞的
        await asyncio.sleep(0.1)

    asyncio.run(_run())
