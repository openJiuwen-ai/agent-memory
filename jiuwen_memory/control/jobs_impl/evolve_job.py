# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EvolveJob——通用演进入口（候选→演进→回显三段，F04 A 形态）。

链条 owner 在本 Job 的 :meth:`EvolveJob.run` 内：resolver 统一候选（四源
唯一汇合点）→ 逐桶排除 middle 后调 ``evolver.evolve(units, mode)`` → 从
outcome 统一翻译 JobInfo 回显。mode 由构造参数注入，Scheduler 不持有
kv/evolver。装配期依赖（真源 KV 端口）固化到 :class:`EvolveJobSpec`；
evolver 由 Engine 经 :class:`JobFactory.get_job` 运行时注入（E-06：Job 与
Engine 共用同一实例，Spec 不自行解析）。

红线（F04 D3）：立即跑与定时跑共用同一个 ``run()``——Engine 不得另写
第二份链条。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.types import JobInfo, JobStatus
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

from .candidate_resolver import CandidateResolver, PredicateResolver


class EvolveJob(Job):
    """通用演进任务。

    ``resolver.resolve()`` 统一候选（默认谓词源 = list scope 全量，现状行为
    的等价物）→ 逐桶演进（middle 排除留在链条——该类 unit 由
    MiddleToLongJob 专门处理，避免同一原文被两次处理）→ outcome 统一回显。
    ``interval=0``：一次性任务（提交后执行一次即完成）。

    纯执行件（PEP 边界，S03）：不做鉴权。持续授权（dreaming 每 tick 复验）
    在 API 层的驱动任务（``DreamingDriverJob``）里做——本 Job 只被它逐次
    提交，任务存续期间授权语义由驱动侧持有。
    """

    def __init__(
        self,
        scope: Scope,
        kv: KVStore,
        evolver: Evolver,
        mode: EvolveMode = EvolveMode.EXTRACT,
        interval: int = 0,
        resolver: CandidateResolver | None = None,
    ) -> None:
        super().__init__(scope=scope, interval=interval)
        self._kv = kv
        self._evolver = evolver
        self._mode = mode
        # None → 默认谓词源（list 全量）——不传 resolver 的旧调用路径行为不变。
        self._resolver = resolver or PredicateResolver(scope=scope, kv=kv)

    @property
    def mode(self) -> str:
        return self._mode.value

    @property
    def schedule_key(self) -> str:
        """调度去重键：同 scope 下不同 mode 的演进是不同任务（含 mode）。

        Scheduler 去重只依赖本键，不感知 EvolveJob 业务字段——"EXTRACT 与
        FORGET 定时器互不覆盖"在这里声明一次，各 Scheduler 实现零业务知识。
        """
        fields = ("org", "space", "user", "agent", "session")
        coords = "/".join(getattr(self.scope, f) or "" for f in fields)
        return f"evolve:{self._mode.value}@{coords}"

    async def run(self) -> JobInfo:
        self.check_cancelled()
        outcome = await self._resolver.resolve()
        created: list[str] = []
        updated: list[str] = []
        superseded: list[str] = []
        forgotten: list[str] = []
        for group in outcome.groups:
            # 空桶仍调 evolver（空 units）——单桶空跑是既有契约（与
            # InProcessScheduler 行为一致）；fan-out 桶来自 scopes()，本身
            # 有数据才存在，空桶罕见。
            units = [
                unit for unit in group.units
                if unit.system_metadata.get("middle") != "true"
            ]
            self.check_cancelled()
            # 在线程实际取得执行机会后再检查，覆盖线程池排队期间的取消。
            result = await asyncio.to_thread(self._evolve_group, units)
            self.check_cancelled()
            created.extend(result.created_ids)
            updated.extend(result.updated_ids)
            superseded.extend(result.superseded_ids)
            forgotten.extend(result.forgotten_ids)
        detail: dict[str, str] = {
            "created_ids": ",".join(created),
            "updated_ids": ",".join(updated),
            "superseded_ids": ",".join(superseded),
            "forgotten_ids": ",".join(forgotten),
            "mode": self._mode.value,
            "groups": str(len(outcome.groups)),
        }
        if outcome.requested_ids is not None:  # ② 点名源差集回显（F04 D3）
            detail["requested_ids"] = ",".join(outcome.requested_ids)
            detail["loaded_ids"] = ",".join(outcome.loaded_ids or [])
            detail["skipped"] = ";".join(outcome.skipped or [])
        if outcome.denied_scopes:  # ④ 枚举源逐桶授权拒绝回显（F04 D7）
            detail["denied_scopes"] = ";".join(outcome.denied_scopes)
        return JobInfo(scope=self.scope, status=JobStatus.SUCCEEDED, detail=detail)

    def _evolve_group(self, units: list[MemoryUnit]) -> EvolveResult:
        self.check_cancelled()
        return self._evolver.evolve(units, self._mode)


# -- Spec + builder ------------------------------------------------------- #


@dataclass
class EvolveJobSpec:
    """EvolveJob 装配期固化的部分——不含 scope/mode/evolver（evolve 调用时补）。

    ``mode`` 是运行时参数（每次 evolve 入参不同），不进 Spec。
    ``evolver`` 同为运行时注入（E-06）：``Engine.evolve`` 经 ``get_job``
    传入装配给 Engine 的同一实例，保证演进与写入使用同一套索引组件；
    缺失时显式报错，不回退默认实现。
    """

    kv: KVStore
    evolver: Evolver | None = None

    def with_scope(self, scope: Scope, **kwargs) -> EvolveJob:
        """生成完整 Job 实例——``kwargs`` 透传运行时参数（``mode`` / ``evolver`` 等）。"""
        evolver = kwargs.pop("evolver", None) or self.evolver
        if evolver is None:
            raise ValidationError(
                "EvolveJob requires an Evolver (由 Engine 经 get_job 运行时注入，"
                "Spec 不自行解析)"
            )
        return EvolveJob(scope=scope, kv=self.kv, evolver=evolver, **kwargs)


def _build_evolve_job_spec(config) -> EvolveJobSpec:
    """装配期固化 EvolveJob 的依赖——返回 Spec dataclass。

    E-06：evolver 不在此解析——Engine.evolve 提交时注入装配给 Engine 的
    同一实例，Job 不得自行解析另一套。
    """
    return EvolveJobSpec(
        kv=StoreManagerProducer.resolve(config).kv(resolve_name(config, "kv_store"))
    )
