"""AsyncTimerScheduler——异步 + 定时调度器。

职责边界：
- 接收外部提交的 Job（不自建 task 内容）；
- 一次性 Job（``interval=0``）：直接入 per scope FIFO 队列；
- 定时 Job（``interval>0``）：注册到 per scope TimerWheel，Timer 协程周期生成实例入队；
- 同 scope 单 drain Task 串行消费 FIFO 队列，跨 scope 完全并行；
- Timer 协程只做"扫一遍 + append"，不抢 drain Task——一次性任务能在 tick 间隙跑。

定时精度上限 = ``tick_interval``：触发实际时刻 ∈ [next_run_at, next_run_at + tick_interval]。
``interval < tick_interval`` 时无法保证触发语义，submit 时校验拒绝。
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.errors import NotFoundError
from common.log import get_logger
from common.type_def import Scope
from control.base import ControlOperatorType
from control.jobs import Job
from control.scheduler import Scheduler, SchedulerProducer
from control.types import Channel, JobInfo, JobStatus

logger = get_logger(__name__)


@dataclass
class TimerEntry:
    """一条定时任务在 TimerWheel 内的注册项。

    不入队——由 ``_timer_loop`` 每 ``tick_interval`` 秒检查 ``next_run_at``，
    到点生成一次性实例塞 queue。
    """

    job_id: str  # 定时任务长生命 id（与一次性实例区分）
    job: Job  # 定时任务定义（用于生成实例）
    interval: int  # 周期长度（秒）
    next_run_at: int  # 下次触发时间戳（秒，int(time.monotonic())）
    is_done: bool = False  # 实例返回 is_done=true 后置位——下次到点不入队


@dataclass
class TimerWheel:
    """per scope 的定时任务集合。

    同一 scope 内多个定时任务（按 kind 区分）共享一个 Timer 协程——
    协程每 ``tick_interval`` 秒遍历 entries 检查 ``next_run_at``。
    """

    scope_key: tuple
    entries: list[TimerEntry] = field(default_factory=list)
    task: asyncio.Task | None = None  # per scope 唯一的 Timer 协程句柄


class AsyncTimerScheduler(Scheduler):
    """异步 + 定时调度器。

    ``tick_interval`` 为 int 秒数——定时精度上限。``interval`` 同样 int，
    与 :class:`~control.jobs.Job` 的 ``interval: int`` 对齐。
    """

    def __init__(self, tick_interval: int = 10) -> None:
        self._tick_interval = tick_interval

        # per scope 三件套：queue（FIFO 队列）+ drain_task（单消费协程）
        # 同 scope 串行性由"per scope 单 drain Task"保证——单线程事件循环 + 单
        # drain 协程跑 FIFO，无并发竞争，无需 asyncio.Lock。
        self._scope_queues: dict[tuple, deque] = defaultdict(deque)
        self._scope_drain_tasks: dict[tuple, asyncio.Task] = {}

        # per scope TimerWheel
        self._wheels: dict[tuple, TimerWheel] = {}

        # job_id → JobInfo（含一次性实例与定时任务的 JobInfo）
        self._jobs: dict[str, JobInfo] = {}

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SCHEDULER

    def health(self) -> None:
        return None

    @staticmethod
    def _scope_key(scope: Scope) -> tuple[str, ...]:
        """per scope dict key——Scheduler 内部实现细节。"""
        return (scope.org, scope.space, scope.user, scope.agent, scope.session)

    # ---- 公开 API ----

    async def submit(self, job: Job, channel: Channel) -> str:
        scope_key = self._scope_key(job.scope)
        if job.interval > 0:
            if job.interval < self._tick_interval:
                raise ValueError(
                    f"interval {job.interval} < tick_interval {self._tick_interval}: "
                    "定时精度无法保证，请增大 interval 或减小 tick_interval"
                )
            return self._submit_timer(scope_key, job, channel)
        return self._submit_once(scope_key, job, channel)

    def status(self, job_id: str) -> JobInfo:
        if job_id not in self._jobs:
            logger.warning("AsyncTimerScheduler.status missing job: job_id=%s", job_id)
            raise NotFoundError("job", job_id)
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        """取消任务（幂等）。

        - 一次性任务：标记 JobInfo.status=CANCELLED（执行中或已完成的忽略）
        - 定时任务：标记 entry.is_done=True + 从 entries 移除（下次 tick 不再触发）
        """
        info = self._jobs.get(job_id)
        if info is not None and info.status == JobStatus.PENDING:
            info.status = JobStatus.CANCELLED
            logger.info("AsyncTimerScheduler.cancelled: job_id=%s", job_id)
            return

        # 定时任务：按 job_id 反查 entry
        for wheel in self._wheels.values():
            for entry in wheel.entries:
                if entry.job_id == job_id:
                    entry.is_done = True
                    wheel.entries.remove(entry)
                    self._jobs[job_id].status = JobStatus.CANCELLED
                    logger.info(
                        "AsyncTimerScheduler.cancelled timer: job_id=%s scope=%s",
                        job_id,
                        wheel.scope_key,
                    )
                    return

    # ---- 一次性任务路径 ----

    def _submit_once(self, scope_key: tuple, job: Job, channel: Channel) -> str:
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = JobInfo(
            id=job_id,
            channel=channel,
            mode=type(job).__name__,
            scope=job.scope,
            status=JobStatus.PENDING,
        )
        self._scope_queues[scope_key].append((job_id, job))
        self._ensure_drain_task(scope_key)
        logger.info(
            "AsyncTimerScheduler.submit_once: job_id=%s scope=%s kind=%s",
            job_id,
            scope_key,
            type(job).__name__,
        )
        return job_id

    def _ensure_drain_task(self, scope_key: tuple) -> None:
        existing = self._scope_drain_tasks.get(scope_key)
        if existing is None or existing.done():
            self._scope_drain_tasks[scope_key] = asyncio.create_task(
                self._drain_queue(scope_key)
            )

    async def _drain_queue(self, scope_key: tuple) -> None:
        """per scope 队列消费：按 FIFO 串行跑 queue 里所有 Job 实例。

        串行性由"per scope 单 drain Task"保证——``_ensure_drain_task``
        检查 ``existing.done()``，旧 drain 没跑完不创建新 drain，故同 scope
        同一时刻只有一个 drain 协程在跑。``await job.run`` 让出事件循环时
        （Job 内部若 to_thread 包了同步算子），Timer 协程能 append 新任务，
        但不会触发第二个 drain——FIFO 顺序消费。
        """
        queue = self._scope_queues[scope_key]
        while queue:
            job_id, job = queue.popleft()
            info = self._jobs[job_id]
            if info.status == JobStatus.CANCELLED:
                continue
            info.status = JobStatus.RUNNING
            info.detail["started_at"] = self._now_iso()
            try:
                result = await job.run()
                self._merge_info(info, result)
                info.status = JobStatus.SUCCEEDED
                logger.info(
                    "AsyncTimerScheduler.succeeded: job_id=%s kind=%s scope=%s",
                    job_id, type(job).__name__, scope_key,
                )
            except asyncio.CancelledError:
                # 事件循环关闭 / 主动 cancel Task——把状态 + 日志打全再重新 raise，
                # 不能吞，否则破坏 asyncio cancel 协议。
                info.status = JobStatus.CANCELLED
                info.detail["cancelled_at"] = self._now_iso()
                logger.info(
                    "AsyncTimerScheduler.cancelled_by_loop_shutdown: job_id=%s kind=%s scope=%s",
                    job_id, type(job).__name__, scope_key,
                )
                raise
            except Exception as exc:
                info.status = JobStatus.FAILED
                info.detail["error_type"] = type(exc).__name__
                info.detail["error"] = str(exc)
                logger.warning(
                    "AsyncTimerScheduler.failed: job_id=%s kind=%s error_type=%s error=%s",
                    job_id, type(job).__name__, type(exc).__name__, exc,
                )
            finally:
                info.detail["finished_at"] = self._now_iso()

    # ---- 定时任务路径 ----

    def _submit_timer(self, scope_key: tuple, job: Job, channel: Channel) -> str:
        wheel = self._wheels.get(scope_key)
        kind = type(job).__name__

        # 同 scope 同 kind 已存在：刷新 job 引用 + 复活 is_done（复用 entry job_id）
        if wheel is not None:
            for entry in wheel.entries:
                if type(entry.job).__name__ == kind:
                    was_done = entry.is_done
                    entry.job = job
                    entry.interval = job.interval
                    entry.is_done = False
                    # was_done=True（之前已退出）→ 重置 next_run_at = now + interval；
                    # was_done=False（定时器还在跑）→ 不动 next_run_at，保持首次
                    # submit 设定的周期节拍，避免连续 write_async 把 next_run_at
                    # 一直往后推导致 Timer 永不触发。
                    if was_done:
                        entry.next_run_at = int(time.monotonic()) + job.interval
                    self._ensure_timer_task(wheel)
                    logger.info(
                        "AsyncTimerScheduler: update timer scope=%s kind=%s interval=%s "
                        "was_done=%s",
                        scope_key,
                        kind,
                        job.interval,
                        was_done,
                    )
                    return entry.job_id

        # 新定时任务——创建 entry 入 wheel
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = JobInfo(
            id=job_id,
            channel=channel,
            mode=kind,
            scope=job.scope,
            status=JobStatus.RUNNING,
            detail={
                "interval": str(job.interval),
                "tick_interval": str(self._tick_interval),
            },
        )
        entry = TimerEntry(
            job_id=job_id,
            job=job,
            interval=job.interval,
            next_run_at=int(time.monotonic()) + job.interval,  # 首次触发要等一个 interval
        )

        if wheel is None:
            wheel = TimerWheel(scope_key=scope_key)
            self._wheels[scope_key] = wheel
            wheel.entries.append(entry)
            wheel.task = asyncio.create_task(self._timer_loop(wheel))
            logger.info(
                "AsyncTimerScheduler: start timer scope=%s kind=%s interval=%s",
                scope_key,
                kind,
                job.interval,
            )
        else:
            wheel.entries.append(entry)  # append 原子（GIL），不抢 lock
            self._ensure_timer_task(wheel)
            logger.info(
                "AsyncTimerScheduler: add timer scope=%s kind=%s interval=%s",
                scope_key,
                kind,
                job.interval,
            )
        return job_id

    def _ensure_timer_task(self, wheel: TimerWheel) -> None:
        """确保 wheel 的 Timer 协程存活——None 或已 done 时重启。"""
        prev = wheel.task
        if prev is None or prev.done():
            wheel.task = asyncio.create_task(self._timer_loop(wheel))
            logger.info(
                "AsyncTimerScheduler: restart timer scope=%s (was %s)",
                wheel.scope_key,
                "None" if prev is None else "done",
            )

    async def _timer_loop(self, wheel: TimerWheel) -> None:
        """per scope 唯一的 Timer 协程：每 tick_interval 秒扫 entries 触发到点任务。

        - 固定 tick_interval 轮询——不依赖最小堆、不依赖 Event 唤醒；
        - 遍历前 snapshot 一份 entries（避免并发迭代器失效）；
        - Timer 协程不持 lock——只做 deque.append（原子），执行由
          ``_drain_queue`` 负责；一次性任务能在 tick 间隙跑；
        - 所有 entry 都 is_done 时退出循环——释放协程，从 _wheels 移除。
        """
        while True:
            await asyncio.sleep(self._tick_interval)
            now = int(time.monotonic())

            # snapshot——遍历中 submit 路径若 append 不影响本帧
            for entry in list(wheel.entries):
                if entry.is_done:
                    continue
                if now >= entry.next_run_at:
                    # 同 scope queue 已有同 kind 实例排队（未开始跑）→ 跳过本次触发，
                    # 防止 run 时长 > interval 时 queue 堆积多个实例串行重跑。
                    kind = type(entry.job).__name__
                    queue = self._scope_queues[wheel.scope_key]
                    if any(type(j).__name__ == kind for _, j in queue):
                        logger.debug(
                            "AsyncTimerScheduler: skip tick kind=%s scope=%s "
                            "(same kind already queued)",
                            kind, wheel.scope_key,
                        )
                        continue
                    # 到点——生成一次性实例塞 queue
                    instance = copy.copy(entry.job)
                    instance.interval = 0
                    instance_id = str(uuid.uuid4())
                    self._jobs[instance_id] = JobInfo(
                        id=instance_id,
                        mode=type(instance).__name__,
                        scope=instance.scope,
                        status=JobStatus.PENDING,
                        detail={"parent_timer": entry.job_id},
                    )
                    self._scope_queues[wheel.scope_key].append((instance_id, instance))
                    self._ensure_drain_task(wheel.scope_key)
                    # 重置下次触发时间
                    entry.next_run_at = now + entry.interval

            # 所有 entry 都 is_done（或 entries 空）——退出
            if not wheel.entries or all(e.is_done for e in wheel.entries):
                logger.info(
                    "AsyncTimerScheduler: wheel all done, exit scope=%s",
                    wheel.scope_key,
                )
                break

            # 清理已 is_done 的 entry——原地修改保持 list 对象引用不失效
            wheel.entries[:] = [e for e in wheel.entries if not e.is_done]

        # Timer 自然退出——标完成
        for entry in wheel.entries:
            self._jobs[entry.job_id].status = JobStatus.SUCCEEDED
            self._jobs[entry.job_id].detail["finished_at"] = self._now_iso()
        self._wheels.pop(wheel.scope_key, None)

    # ---- 辅助 ----

    def _merge_info(self, info: JobInfo, result: JobInfo) -> None:
        """把实例 run() 返回的 JobInfo 合并回主 info，并处理 is_done 传播。"""
        for k, v in result.detail.items():
            if k == "is_done" and v == "true":
                # 实例返回 is_done=true：通知对应 entry 停止下一轮触发
                parent_id = info.detail.get("parent_timer")
                if parent_id:
                    self._mark_timer_done(parent_id)
            info.detail[k] = v

    def _mark_timer_done(self, parent_timer_id: str) -> None:
        """按 parent_timer_id 反查 entry 标记 is_done——下次 tick 跳过。"""
        for wheel in self._wheels.values():
            for entry in wheel.entries:
                if entry.job_id == parent_timer_id:
                    entry.is_done = True
                    return

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


# -- 注册到 SchedulerProducer（实现自注册） --------------------------------- #


@SchedulerProducer.register("async_timer")
def _build_async_timer(config):
    tick = int(config.get("tick_interval", 10))
    return AsyncTimerScheduler(tick_interval=tick)
