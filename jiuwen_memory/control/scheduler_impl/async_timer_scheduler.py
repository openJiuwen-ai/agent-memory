# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AsyncTimerScheduler——异步 + 定时调度器。

职责边界：
- 接收外部提交的 Job（不自建 task 内容）；
- 一次性 Job（``interval=0``）：直接入 per scope FIFO 队列；
- 定时 Job（``interval>0``）：注册到 per scope TimerWheel，Timer 协程周期生成实例入队；
- 同 scope 单 drain Task 串行消费 FIFO 队列，跨 scope 完全并行；
- Timer 协程只做"扫一遍 + append"，不抢 drain Task——一次性任务能在 tick 间隙跑。

**自有事件循环（单实例部署模型的运行时支柱）**：Timer / drain 协程不能寄生在
调用方的事件循环上——API 入口（HTTP/SDK）是每请求 ``asyncio.run`` 的短命循环，
循环一关协程即死，注册态任务（dreaming）会静默停摆。本调度器首次 ``submit``
惰性启动一条**私有后台事件循环线程**（daemon），全部协程栖身其上；外部循环
经 ``submit`` 提交时自动跨线程 marshal（``run_coroutine_threadsafe``），
调用方循环关闭不再影响已注册任务。``shutdown`` 负责停线程（注册态任务的
持久化注册表不受影响，重启后 ``restore_dreaming`` 重新装配）。

定时精度上限 = ``tick_interval``：触发实际时刻 ∈ [next_run_at, next_run_at + tick_interval]。
``interval < tick_interval`` 时无法保证触发语义，submit 时校验拒绝。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jiuwen_memory.common.errors import NotFoundError, PermissionDeniedError
from jiuwen_memory.common.log import (
    get_logger,
    metadata_for_log,
    scope_for_log,
)
from jiuwen_memory.common.loop_runner import LoopRunner
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.jobs import Job, JobCancelledError
from jiuwen_memory.control.scheduler import (
    RecurringJobStoppedError,
    Scheduler,
    SchedulerProducer,
)
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

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

    同一 scope 内多个定时任务（按 ``job.schedule_key`` 区分）共享一个
    Timer 协程——协程每 ``tick_interval`` 秒遍历 entries 检查 ``next_run_at``。
    """

    scope_key: tuple
    entries: list[TimerEntry] = field(default_factory=list)
    task: asyncio.Task | None = None  # per scope 唯一的 Timer 协程句柄


class AsyncTimerScheduler(Scheduler):
    """异步 + 定时调度器。

    ``tick_interval`` 为 int 秒数——定时精度上限。``interval`` 同样 int，
    与 :class:`~control.jobs.Job` 的 ``interval: int`` 对齐。

    Timer/drain Task 绑定在 Scheduler 自有的常驻守护 loop 上（``LoopRunner``），
    与调用方 loop 完全解耦——sync 写入经 ``asyncio.run`` 建临时 loop、返回即
    关闭的历史问题（绑在临时 loop 上的 Timer 随之消亡、中期转换永不触发）
    由此外科式消除。submit/cancel 跨 loop 提交到守护 loop，所有状态变更
    收敛到单 loop 单线程，per scope 串行性保证更纯粹。
    """

    def __init__(self, tick_interval: int = 10) -> None:
        self._tick_interval = tick_interval

        # 常驻守护 loop——Timer/drain Task 的宿主，随进程存活（daemon 线程）。
        self._runner = LoopRunner(thread_name="agent-memory-scheduler-loop")

        # per scope 三件套：queue（FIFO 队列）+ drain_task（单消费协程）
        # 同 scope 串行性由"per scope 单 drain Task"保证——单线程事件循环 + 单
        # drain 协程跑 FIFO，无并发竞争，无需 asyncio.Lock。
        self._scope_queues: dict[tuple, deque] = defaultdict(deque)
        self._scope_drain_tasks: dict[tuple, asyncio.Task] = {}

        # per scope TimerWheel
        self._wheels: dict[tuple, TimerWheel] = {}

        # job_id → JobInfo（含一次性实例与定时任务的 JobInfo）
        self._jobs: dict[str, JobInfo] = {}
        self._live_jobs: dict[str, Job] = {}
        self._completed_ids: deque[str] = deque()
        self._completed_id_set: set[str] = set()
        self._max_completed_jobs = 10_000
        # 所有由本调度器创建的协程任务都在这里留强引用。wheel.task 等业务
        # 索引可能在恢复/测试场景被替换，shutdown 仍必须能找到旧任务并收尾。
        self._background_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _scope_key(scope: Scope) -> tuple[str, ...]:
        """per scope dict key——Scheduler 内部实现细节。"""
        return (scope.org, scope.space, scope.user, scope.agent, scope.session)

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.SCHEDULER

    @property
    def supports_recurring(self) -> bool:
        return True

    def health(self) -> None:
        return None

    # ---- 公开 API ----

    def validate(self, job: Job) -> None:
        """interval < tick_interval 时定时精度无法保证——submit 前拒绝。

        与 :meth:`submit` 共用同一校验——Engine 落盘前先调本方法 fail fast，
        避免「submit 拒绝但原文已落盘」的残留窗口。
        """
        if job.interval > 0 and job.interval < self._tick_interval:
            raise ValueError(
                f"interval {job.interval} < tick_interval {self._tick_interval}: "
                "定时精度无法保证，请增大 interval 或减小 tick_interval"
            )

    async def submit(self, job: Job, channel: Channel) -> str:
        # 先在调用方上下文 fail fast——校验是纯同步逻辑，不依赖 loop。
        self.validate(job)
        loop = self._runner.ensure_loop()
        # DriverJob 会在守护 loop 内提交派生 EvolveJob。此时必须直接 await 实际
        # 提交体；再用 run_coroutine_threadsafe 会让当前 loop 等待自己而死锁。
        if asyncio.get_running_loop() is loop:
            return await self._submit_on_loop(job, channel)
        # 跨 loop 提交到守护 loop：_submit_on_loop 内部的 create_task 落在守护
        # loop 上，Timer/drain 生命周期与调用方 loop（可能是 asyncio.run 的
        # 临时 loop）解耦。async 调用方经 wrap_future 在自己的 loop 上等待。
        future = asyncio.run_coroutine_threadsafe(
            self._submit_on_loop(job, channel), loop
        )
        return await asyncio.wrap_future(future)

    async def _submit_on_loop(self, job: Job, channel: Channel) -> str:
        """守护 loop 上执行的实际提交逻辑（原 submit 主体）。"""
        scope_key = self._scope_key(job.scope)
        if job.interval > 0:
            return self._submit_timer(scope_key, job, channel)
        return self._submit_once(scope_key, job, channel)

    def status(self, job_id: str) -> JobInfo:
        return self._runner.run(self._status_snapshot(job_id), timeout=10)

    async def _status_snapshot(self, job_id: str) -> JobInfo:
        return self._status_snapshot_now(job_id)

    def _status_snapshot_now(self, job_id: str) -> JobInfo:
        if job_id not in self._jobs:
            logger.warning("AsyncTimerScheduler.status missing job: job_id=%s", job_id)
            raise NotFoundError("job", job_id)
        return copy.deepcopy(self._jobs[job_id])

    def cancel(self, job_id: str) -> None:
        """取消任务（幂等）。

        - 一次性任务：标记 JobInfo.status=CANCELLED（执行中或已完成的忽略）
        - 定时任务：移除 entry，并通知已有实例/派生任务停止尚未开始的步骤

        串行化到守护 loop——cancel 的 entries.remove 与 Timer 协程的
        entries 原地清理若在不同线程并发执行存在竞态，收敛到单 loop 消除。

        .. warning::
            本方法**阻塞调用线程**直到守护 loop 上的取消协程完成，**不可从守护
            loop 自身的协程内调用**（如 drain task 里取消另一个 job）——
            ``run_coroutine_threadsafe`` 提交的协程要等当前协程让出才能被调度，
            而当前线程正阻塞在 ``future.result()`` 上，互相等待即死锁。
            仅支持从守护 loop 之外的外部 sync 上下文调用（HTTP handler 线程、
            调用方主线程 / 临时请求 loop 等）。在守护 loop 协程内取消请直接
            ``await self._cancel_on_loop(job_id)``。
        """
        self._runner.run(self._cancel_on_loop(job_id), timeout=10)

    def link_child(self, child_job_id: str, parent_job_id: str) -> None:
        # DriverJob 在守护 loop 内调用；同 loop 路径必须同步完成关联，保证子任务
        # 尚未开始前已继承父任务的取消信号。
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._runner.ensure_loop():
            self._link_child_now(child_job_id, parent_job_id)
        else:
            self._runner.run(
                self._link_child_on_loop(child_job_id, parent_job_id), timeout=10
            )

    async def _link_child_on_loop(self, child_job_id: str, parent_job_id: str) -> None:
        self._link_child_now(child_job_id, parent_job_id)

    def _link_child_now(self, child_job_id: str, parent_job_id: str) -> None:
        child = self._jobs.get(child_job_id)
        parent = self._jobs.get(parent_job_id)
        if child is None or parent is None:
            return
        child.detail["parent_timer"] = parent_job_id
        parent.detail["last_job_id"] = child_job_id
        child_job = self._live_jobs.get(child_job_id)
        parent_job = self._live_jobs.get(parent_job_id)
        if child_job is not None:
            child_job.parent_job_id = parent_job_id
            if parent_job is not None:
                child_job.inherit_cancellation_from(parent_job)
            elif parent.status != JobStatus.RUNNING:
                child_job.request_cancel()

    async def _cancel_on_loop(self, job_id: str) -> None:
        self._cancel_now(job_id)

    def _cancel_now(self, job_id: str) -> None:
        info = self._jobs.get(job_id)
        if info is not None and info.status == JobStatus.PENDING:
            info.status = JobStatus.CANCELLED
            logger.info("AsyncTimerScheduler.cancelled: job_id=%s", job_id)
            return

        # 定时任务：按 job_id 反查 entry（snapshot 迭代——私有循环可能并发
        # append 新 entry，跨线程迭代原 list 会 RuntimeError）
        for wheel in self._wheels.values():
            for entry in list(wheel.entries):
                if entry.job_id == job_id:
                    entry.job.request_cancel()
                    self._live_jobs.pop(job_id, None)
                    entry.is_done = True
                    wheel.entries.remove(entry)
                    self._jobs[job_id].status = JobStatus.CANCELLED
                    self._remember_completed(job_id)
                    logger.info(
                        "AsyncTimerScheduler.cancelled timer: job_id=%s scope=%s",
                        job_id,
                        scope_for_log(wheel.scope_key),
                    )
                    return

    def shutdown(self) -> None:
        """优雅关闭：在守护循环上取消全部 Timer / drain 协程。

        ``Task.cancel`` 非线程安全——必须 marshal 到私有循环执行，不能从
        调用线程直接 cancel。正在跑的实例进入 CANCELLED（CancelledError 分支
        已把状态与日志打全）；``_jobs`` / ``_wheels`` 状态记录保留至进程退出，
        注册表在 KV 里的持久化不受影响——重启后 ``restore_dreaming`` 重新装配。
        LoopRunner 的 daemon 线程由进程管理，本方法只清理由 Scheduler 创建的任务。幂等。
        """
        try:
            self._runner.run(self._cancel_all_tasks(), timeout=10)
        except concurrent.futures.TimeoutError:
            logger.error("AsyncTimerScheduler.shutdown timed out waiting for tasks")
        logger.info("AsyncTimerScheduler.shutdown: timers and drains cancelled")

    async def _cancel_all_tasks(self) -> None:
        """在私有循环内取消全部 Timer 协程与 drain Task（shutdown 用）。"""
        tasks = [task for task in self._background_tasks if not task.done()]
        for job in self._live_jobs.values():
            job.request_cancel()
        for task in tasks:
            task.cancel()
        self._wheels.clear()
        self._scope_drain_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._live_jobs.clear()

    # ---- 一次性任务路径 ----

    def _submit_once(self, scope_key: tuple, job: Job, channel: Channel) -> str:
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = JobInfo(
            id=job_id,
            channel=channel,
            # 与 InProcessScheduler 同口径：优先取演进模式，无则回落任务类名。
            mode=job.mode or type(job).__name__,
            scope=job.scope,
            status=JobStatus.PENDING,
        )
        self._scope_queues[scope_key].append((job_id, job))
        self._live_jobs[job_id] = job
        self._ensure_drain_task(scope_key)
        logger.info(
            "AsyncTimerScheduler.submit_once: job_id=%s scope=%s kind=%s",
            job_id,
            scope_for_log(scope_key),
            type(job).__name__,
        )
        return job_id

    def _ensure_drain_task(self, scope_key: tuple) -> None:
        existing = self._scope_drain_tasks.get(scope_key)
        if existing is None or existing.done():
            self._scope_drain_tasks[scope_key] = self._create_task(
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
                self._live_jobs.pop(job_id, None)
                self._remember_completed(job_id)
                continue
            info.status = JobStatus.RUNNING
            info.detail["started_at"] = self._now_iso()
            try:
                job.check_cancelled()
                result = await job.run()
                self._merge_info(info, result)
                info.status = JobStatus.SUCCEEDED
                logger.info(
                    "AsyncTimerScheduler.succeeded: job_id=%s kind=%s scope=%s",
                    job_id,
                    type(job).__name__,
                    scope_for_log(scope_key),
                )
            except JobCancelledError:
                info.status = JobStatus.CANCELLED
                info.detail["cancelled_at"] = self._now_iso()
            except asyncio.CancelledError:
                # 事件循环关闭 / 主动 cancel Task——把状态 + 日志打全再重新 raise，
                # 不能吞，否则破坏 asyncio cancel 协议。
                info.status = JobStatus.CANCELLED
                info.detail["cancelled_at"] = self._now_iso()
                logger.info(
                    "AsyncTimerScheduler.cancelled_by_loop_shutdown: job_id=%s kind=%s scope=%s",
                    job_id,
                    type(job).__name__,
                    scope_for_log(scope_key),
                )
                raise
            except PermissionDeniedError as exc:
                # 持续授权拒绝（dreaming 每 tick 复验失败等）——停掉父定时器，
                # 本实例记 FAILED；不 re-raise：drain Task 必须活着消费同 scope
                # 后续队列，注册表清理由分发层守卫闭包负责（拒绝时已 remove）。
                info.status = JobStatus.FAILED
                info.detail["error_type"] = type(exc).__name__
                info.detail["error"] = str(exc)
                parent_id = info.detail.get("parent_timer")
                if parent_id:
                    self._mark_timer_done(parent_id)
                    # 显式存在性检查：parent_id 不在 _jobs 时不得回落到当前
                    # 实例的 info——否则父定时器的 stopped_reason 会错写到
                    # 本实例（已是 FAILED）上，产生误导性状态记录。
                    parent_info = self._jobs.get(parent_id)
                    if parent_info is not None:
                        # 置 FAILED 而非留在 RUNNING：父定时器已停摆，RUNNING
                        # 是"还在跑"的错误信号；_timer_loop 退出路径对非 RUNNING
                        # 状态不覆写 SUCCEEDED（见该处注释）。
                        parent_info.status = JobStatus.FAILED
                        parent_info.detail["stopped_reason"] = "permission_denied"
                logger.warning(
                    "AsyncTimerScheduler.permission_denied: job_id=%s kind=%s scope=%s",
                    job_id, type(job).__name__, scope_key,
                )
            except RecurringJobStoppedError as exc:
                info.status = JobStatus.FAILED
                info.detail["error_type"] = type(exc).__name__
                info.detail["error"] = str(exc)
                parent_id = info.detail.get("parent_timer")
                if parent_id:
                    self._mark_timer_done(parent_id)
                    parent_info = self._jobs.get(parent_id)
                    if parent_info is not None:
                        parent_info.status = JobStatus.FAILED
                        parent_info.detail["stopped_reason"] = exc.reason
                logger.error(
                    "AsyncTimerScheduler.recurring_stopped: job_id=%s reason=%s",
                    job_id,
                    exc.reason,
                )
            except Exception as exc:
                info.status = JobStatus.FAILED
                info.detail["error_type"] = type(exc).__name__
                info.detail["error"] = str(exc)
                logger.warning(
                    "AsyncTimerScheduler.failed: job_id=%s kind=%s error_type=%s",
                    job_id,
                    type(job).__name__,
                    type(exc).__name__,
                )
            finally:
                info.detail["finished_at"] = self._now_iso()
                parent_id = info.detail.get("parent_timer")
                parent = self._jobs.get(parent_id) if parent_id else None
                if parent is not None:
                    parent.detail["last_run_at"] = info.detail["finished_at"]
                    parent.detail["last_run_status"] = info.status.value
                    if info.status == JobStatus.SUCCEEDED:
                        parent.detail["last_success_at"] = info.detail["finished_at"]
                        parent.detail.pop("last_error", None)
                    elif "error" in info.detail:
                        parent.detail["last_error"] = info.detail["error"]
                self._remember_completed(job_id)
                self._live_jobs.pop(job_id, None)

    # ---- 定时任务路径 ----

    def _submit_timer(self, scope_key: tuple, job: Job, channel: Channel) -> str:
        wheel = self._wheels.get(scope_key)
        schedule_key = job.schedule_key
        kind = type(job).__name__

        # 同 scope 同 schedule_key 已存在：刷新 job 引用 + 复活 is_done（复用 entry job_id）。
        # 去重只依赖 Job 契约的通用键——Scheduler 不识别 EvolveJob 或其 mode 业务
        # 语义；"同 scope 下 EXTRACT 与 FORGET 是不同任务"由 EvolveJob 的
        # schedule_key（含 mode）表达，这里零业务知识。
        if wheel is not None:
            for entry in wheel.entries:
                if entry.job.schedule_key == schedule_key:
                    was_done = entry.is_done
                    interval_changed = job.interval != entry.interval
                    # was_done=True：复活，重置为首次 submit 语义。
                    # was_done=False + interval 变化：next_run_at - 旧 interval + 新 interval
                    #   （next_run_at 自身编码"基准时间 + 旧 interval"，减旧即得基准时间）。
                    # was_done=False + interval 不变：不动 next_run_at（debounce 保护——
                    #   连续 add_async 不应把 Timer 一直推后）。
                    if was_done:
                        entry.next_run_at = int(time.monotonic()) + job.interval
                    elif interval_changed:
                        entry.next_run_at = (
                            entry.next_run_at - entry.interval + job.interval
                        )
                    if not was_done:
                        job.inherit_cancellation_from(entry.job)
                    entry.job = job
                    self._live_jobs[entry.job_id] = job
                    entry.interval = job.interval
                    entry.is_done = False
                    # JobInfo 与 entry 同步刷新：去重键是 schedule_key，而 JobInfo 的
                    # mode 取实例声明的演进模式，取值随实例变化而键不随；status 也会漂——
                    # 上一轮结束时置 SUCCEEDED、取消时置 CANCELLED，复活后不复位即
                    # 「任务在跑而 job_status 报终态」。鉴权点按 mode 决定 job_status /
                    # job_cancel 要哪个动作，漂移方向为放行（WRITE 宽于 UPDATE）。
                    info = self._jobs.get(entry.job_id)
                    if info is not None:
                        info.mode = job.mode or type(job).__name__
                        info.scope = job.scope
                        info.status = JobStatus.RUNNING
                        info.detail["interval"] = str(job.interval)
                        # 清上一生命周期的停摆痕迹（拒绝停摆后一个 tick 窗口内
                        # 重新注册的场景）：status 已复活 RUNNING，残留的
                        # stopped_reason 会让 job_status 回显"在跑却带着停摆
                        # 原因"的自相矛盾状态。
                        info.detail.pop("stopped_reason", None)
                        info.detail.pop("finished_at", None)
                    self._ensure_timer_task(wheel)
                    timer_metadata = {
                        "effective_interval": job.interval,
                        "interval_changed": interval_changed,
                    }
                    logger.info(
                        "AsyncTimerScheduler: update timer scope=%s kind=%s "
                        "was_done=%s scheduler_metadata=%s",
                        scope_for_log(scope_key),
                        kind,
                        was_done,
                        metadata_for_log(timer_metadata),
                    )
                    return entry.job_id

        # 新定时任务——创建 entry 入 wheel
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = JobInfo(
            id=job_id,
            channel=channel,
            # 与一次性任务路径 / InProcessScheduler 同口径：优先取演进模式，
            # 无则回落任务类名（回落源必须全局一致——类名与 schedule_key 的
            # 取值不同，混用会让鉴权点对同一任务在不同路径看到不同 mode）。
            mode=job.mode or type(job).__name__,
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
        self._live_jobs[job_id] = job

        if wheel is None:
            wheel = TimerWheel(scope_key=scope_key)
            self._wheels[scope_key] = wheel
            wheel.entries.append(entry)
            wheel.task = self._create_task(self._timer_loop(wheel))
            timer_metadata = {"effective_interval": job.interval}
            logger.info(
                "AsyncTimerScheduler: start timer scope=%s kind=%s scheduler_metadata=%s",
                scope_for_log(scope_key),
                kind,
                metadata_for_log(timer_metadata),
            )
        else:
            wheel.entries.append(entry)  # append 原子（GIL），不抢 lock
            self._ensure_timer_task(wheel)
            timer_metadata = {"effective_interval": job.interval}
            logger.info(
                "AsyncTimerScheduler: add timer scope=%s kind=%s scheduler_metadata=%s",
                scope_for_log(scope_key),
                kind,
                metadata_for_log(timer_metadata),
            )
        return job_id

    def _ensure_timer_task(self, wheel: TimerWheel) -> None:
        """确保 wheel 的 Timer 协程存活——None 或已 done 时重启。"""
        prev = wheel.task
        if prev is None or prev.done():
            wheel.task = self._create_task(self._timer_loop(wheel))
            logger.info(
                "AsyncTimerScheduler: restart timer scope=%s (was %s)",
                scope_for_log(wheel.scope_key),
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
                    # 同 scope queue 已有同 schedule_key 实例排队（未开始跑）→ 跳过
                    # 本次触发，防止 run 时长 > interval 时 queue 堆积多个实例串行重跑。
                    # 跳过判定与 _submit_timer 去重键同口径（schedule_key）：同 scope
                    # 排队的 EXTRACT 实例不挡 FORGET 触发。
                    queue = self._scope_queues[wheel.scope_key]
                    if any(
                        j.schedule_key == entry.job.schedule_key
                        for _, j in queue
                    ):
                        logger.debug(
                            "AsyncTimerScheduler: skip tick kind=%s scope=%s "
                            "(same task already queued)",
                            type(entry.job).__name__,
                            scope_for_log(wheel.scope_key),
                        )
                        continue
                    # 到点——生成一次性实例塞 queue
                    instance = copy.copy(entry.job)
                    instance.interval = 0
                    instance.parent_job_id = entry.job_id
                    instance_id = str(uuid.uuid4())
                    self._live_jobs[instance_id] = instance
                    self._jobs[instance_id] = JobInfo(
                        id=instance_id,
                        # 到点生成的实例继承声明时的演进模式，否则遗忘类任务的实例
                        # 会因取值不可解析而回落到 WRITE 动作，比应取的 UPDATE 宽松。
                        mode=instance.mode or type(instance).__name__,
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
                    scope_for_log(wheel.scope_key),
                )
                break

            # 清理已 is_done 的 entry——原地修改保持 list 对象引用不失效
            wheel.entries[:] = [e for e in wheel.entries if not e.is_done]

        # Timer 自然退出——标完成。只覆盖 RUNNING：FAILED / CANCELLED 等终态
        # 由产生它们的路径负责（授权拒绝停摆的 entry 在 _drain_queue 的
        # PermissionDeniedError 分支已置 FAILED + stopped_reason，这里再
        # 无条件写 SUCCEEDED 会把"被拒停"回显成"成功跑完"——自相矛盾）。
        for entry in wheel.entries:
            info = self._jobs.get(entry.job_id)
            if info is None:
                continue
            if info.status == JobStatus.RUNNING:
                info.status = JobStatus.SUCCEEDED
            info.detail.setdefault("finished_at", self._now_iso())
            self._remember_completed(entry.job_id)
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
                    entry.job.request_cancel()
                    self._live_jobs.pop(parent_timer_id, None)
                    entry.is_done = True
                    return

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _create_task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _remember_completed(self, job_id: str) -> None:
        """保留有界的任务历史（含终态周期父任务）。"""
        if job_id in self._completed_id_set:
            return
        self._completed_ids.append(job_id)
        self._completed_id_set.add(job_id)
        while len(self._completed_ids) > self._max_completed_jobs:
            expired = self._completed_ids.popleft()
            self._completed_id_set.discard(expired)
            self._jobs.pop(expired, None)


# -- 注册到 SchedulerProducer（实现自注册） --------------------------------- #


@SchedulerProducer.register("async_timer")
def _build_async_timer(config):
    tick = int(config.get("tick_interval", 10))
    return AsyncTimerScheduler(tick_interval=tick)
