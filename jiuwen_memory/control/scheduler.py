# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scheduler — 演进调度（架构 §8 双通道）。

控制层驱动构建层自演进的调度面：hot 通道做低时延的在线轻量更新，
background 通道异步做重的抽取/升华/重索引，不阻塞主链路。

Scheduler 只调度，不决定 task 内容——task 内容由 :class:`~control.jobs.Job`
封装（"做什么 + 怎么找数据 + 怎么调 evolver + 怎么后处理"）：

- ``interval=0``：一次性任务，submit 时直接入 per scope FIFO 队列
- ``interval>0``：定时任务声明，submit 时注册到 per scope TimerWheel
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory

from .base import ControlOperator
from .jobs import Job
from .types import Channel, JobInfo


class RecurringJobStoppedError(RuntimeError):
    """周期任务遇到不可重试的运行条件，要求 Scheduler 停止父定时器。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SchedulerProducer(Factory):
    """Scheduler 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``scheduler_impl`` 下以
    ``@SchedulerProducer.register("<名>")`` 自注册——注册发生在 import 实现模块
    时，由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "scheduler"


class Scheduler(ControlOperator):
    def validate(self, job: Job) -> None:
        """提交前校验 job 的可调度性，不可调度时抛错——默认无约束。

        供 Engine 在产生副作用（落盘等）之前 fail fast；
        实现应与 :meth:`submit` 内部校验保持同一逻辑。
        """
        return None

    @property
    def supports_recurring(self) -> bool:
        """本调度器是否支持周期任务（``interval>0`` 的真实定时触发）。

        声明能力而非猜实现：dreaming 注册（定时任务）要求本值为 True，
        不支持时注册处 fail fast——否则"注册了定时任务但实际只同步跑了一次"
        这类静默降级要到很久之后才被发现。默认 False（fail closed）。
        """
        return False

    def shutdown(self) -> None:
        """优雅关闭：取消定时/消费协程，已提交任务的状态记录保留。

        默认无协程可关（同步实现）。注册态任务的**持久化注册表不受影响**——
        重启后经 ``restore_dreaming`` 重新装配。
        """
        return None

    def link_child(self, child_job_id: str, parent_job_id: str) -> None:
        """关联派生任务的状态与取消生命周期；不支持周期任务的实现可忽略。

        周期调度器须让派生任务继承父任务的协作式取消信号。调用方须在子任务
        开始执行前完成关联；同一调度循环中 submit 返回后立即调用不得让出循环。
        """
        return None

    @abstractmethod
    async def submit(self, job: Job, channel: Channel) -> str:
        """提交一次任务（指定通道），返回 job_id。

        - ``job.interval=0``：一次性任务，直接入 per scope FIFO 队列
        - ``job.interval>0``：定时任务声明，注册到 per scope TimerWheel

        ``async`` 签名——让调用方(Engine.write/evolve)在事件循环内 ``await submit``,
        submit 内部可 ``await job.run()`` 直接执行(InProcessScheduler)或
        ``asyncio.create_task`` 排程(AsyncTimerScheduler)。
        """

    @abstractmethod
    def status(self, job_id: str) -> JobInfo:
        """查询任务状态。"""

    @abstractmethod
    def cancel(self, job_id: str) -> None:
        """取消尚未完成的任务（幂等）。"""
