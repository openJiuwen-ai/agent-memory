# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EvolveJob——通用演进入口。

数据来源、调用方式与原 ``InProcessScheduler._execute_task`` 一致——
mode 由构造参数注入，Scheduler 不再持有 kv/evolver。装配期依赖
（storage）固化到 :class:`EvolveJobSpec`；evolver 由 Engine 经
:class:`JobFactory.get_job` 运行时注入（E-06：Job 与 Engine 共用同一
实例，Spec 不自行解析）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.construction import EvolveMode, Evolver
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.types import JobInfo, JobStatus
from jiuwen_memory.storage.storage import Storage, StorageProducer


class EvolveJob(Job):
    """通用演进任务。

    list scope 全部 MemoryUnit → ``evolver.evolve(units, mode)``。
    ``interval=0``：一次性任务（提交后执行一次即完成）。
    """

    def __init__(
        self,
        scope: Scope,
        storage: Storage,
        evolver: Evolver,
        mode: EvolveMode = EvolveMode.EXTRACT,
        interval: int = 0,
    ) -> None:
        super().__init__(scope=scope, interval=interval)
        self._storage = storage
        self._evolver = evolver
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode.value

    async def run(self) -> JobInfo:
        # 排除中期记忆：middle 路径写入的 unit 由 MiddleToLongJob 专门处理，
        # 避免同一原文被两次处理。
        page = await asyncio.to_thread(self._storage.list, self.scope, limit=1_000_000)
        units = [
            unit for unit in page.items if unit.system_metadata.get("middle") != "true"
        ]
        result = await asyncio.to_thread(self._evolver.evolve, units, self._mode)
        return JobInfo(
            scope=self.scope,
            status=JobStatus.SUCCEEDED,
            detail={
                "created_ids": ",".join(result.created_ids),
                "updated_ids": ",".join(result.updated_ids),
                "superseded_ids": ",".join(result.superseded_ids),
                "forgotten_ids": ",".join(result.forgotten_ids),
                "mode": self._mode.value,
            },
        )


# -- Spec + builder ------------------------------------------------------- #


@dataclass
class EvolveJobSpec:
    """EvolveJob 装配期固化的部分——不含 scope/mode/evolver（evolve 调用时补）。

    ``mode`` 是运行时参数（每次 evolve 入参不同），不进 Spec。
    ``evolver`` 同为运行时注入（E-06）：``Engine.evolve`` 经 ``get_job``
    传入装配给 Engine 的同一实例，保证演进与写入使用同一套索引组件；
    缺失时显式报错，不回退默认实现。
    """

    storage: Storage
    evolver: Evolver | None = None

    def with_scope(self, scope: Scope, **kwargs) -> EvolveJob:
        """生成完整 Job 实例——``kwargs`` 透传运行时参数（``mode`` / ``evolver`` 等）。"""
        evolver = kwargs.pop("evolver", None) or self.evolver
        if evolver is None:
            raise ValidationError(
                "EvolveJob requires an Evolver (由 Engine 经 get_job 运行时注入，"
                "Spec 不自行解析)"
            )
        return EvolveJob(scope=scope, storage=self.storage, evolver=evolver, **kwargs)


def _build_evolve_job_spec(config) -> EvolveJobSpec:
    """装配期固化 EvolveJob 的依赖——返回 Spec dataclass。

    E-06：evolver 不在此解析——Engine.evolve 提交时注入装配给 Engine 的
    同一实例，Job 不得自行解析另一套。
    """
    return EvolveJobSpec(storage=StorageProducer.resolve(config))
