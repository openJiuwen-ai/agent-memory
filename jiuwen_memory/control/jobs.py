# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Job — 控制层任务抽象。

Job 封装"做什么 + 怎么找数据 + 怎么调 evolver + 怎么后处理"，本身不携带
"何时跑"——何时跑由 Scheduler 决定：

- ``interval=0``：一次性任务，submit 时直接入 per scope FIFO 队列
- ``interval>0``：定时任务声明，submit 时注册到 per scope TimerWheel

本类不自带循环——周期由 Scheduler 的 Timer 协程负责。本模块同时定义
:class:`JobFactory`——按 ``job_type + scope`` 生成 Job 实例。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope

from .types import JobInfo


@dataclass
class Job(ABC):
    """控制层任务抽象基类。

    子类实现 :meth:`run`：拉数据 → 调 evolver → 处理结果，处理一次即返回。
    周期由 Scheduler 的 Timer 协程负责。``run`` 为 ``async`` 接口——Job 内部
    若需调同步阻塞算子（evolver.evolve / LLM.chat）应包
    ``await asyncio.to_thread(...)`` 避免阻塞事件循环。
    """

    scope: Scope = field(default_factory=Scope)
    interval: int = 0  # 0=一次性任务；>0=定时任务声明（秒，须 >= scheduler.tick_interval）

    @abstractmethod
    async def run(self) -> JobInfo:
        """执行任务，返回 JobInfo。"""


class JobType(str, Enum):
    """Job 类型枚举——``JobFactory.get_job`` 的必选参数。"""

    EVOLVE = "evolve"
    MIDDLE_TO_LONG = "middle_to_long"


class JobFactory:
    """通用 Job 工厂——按 ``job_type + scope + 可选参数`` 生成 Job 实例。

    装配期把各 Job 类型的 builder 闭包注册进来——builder 内部固化装配期依赖
    （kv/evolver/llm 等）与业务参数（max_fetch 等），运行时只补 scope 与
    运行时参数（interval/mode 等）。
    """

    def __init__(self) -> None:
        # builder 签名：``builder(scope: Scope, **runtime_kwargs) -> Job``
        self._builders: dict[JobType, Callable[..., Job]] = {}

    def register(self, job_type: JobType, builder: Callable[..., Job]) -> None:
        """装配期注册——builder 闭包固化依赖与业务参数。"""
        if job_type in self._builders:
            raise ValueError(f"JobType {job_type} already registered")
        self._builders[job_type] = builder

    def get_job(self, job_type: JobType, scope: Scope, **kwargs) -> Job:
        """运行时取 Job 实例——按 ``job_type`` 找 builder，传 ``scope + 可选参数``。

        ``kwargs`` 透传运行时参数（如 ``interval`` / ``mode``），装配期固化的
        依赖与业务参数已在 builder 闭包内，不在此传。
        """
        builder = self._builders.get(job_type)
        if builder is None:
            raise ValueError(
                f"JobType {job_type} not registered in JobFactory; "
                f"available: {list(self._builders.keys())}"
            )
        return builder(scope=scope, **kwargs)


class JobFactoryProducer(Factory):
    """JobFactory 的注册式工厂——与 ``EvolverProducer`` 同模式，走装配链。"""

    TOP_NAME = "job_factory"
