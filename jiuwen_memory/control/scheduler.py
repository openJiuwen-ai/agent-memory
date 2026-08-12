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


class SchedulerProducer(Factory):
    """Scheduler 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即实现名。各实现在 ``scheduler_impl`` 下以 ``@SchedulerProducer.register("<名>")`` 自注册——
    注册发生在 import 实现模块时，由 :func:`control.bootstrap.register_controllers` 统一触发。
    """

    TOP_NAME = "scheduler"


class Scheduler(ControlOperator):
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
