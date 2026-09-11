# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Engine 共用的周期注册；不创建线程、临时循环或匿名内容算子。"""

from copy import deepcopy
from dataclasses import dataclass

from jiuwen_memory.common.errors import PolicyError, ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, Scope
from jiuwen_memory.construction.evolver import Evolver
from jiuwen_memory.control.evolution.validation import scope_contains
from jiuwen_memory.control.jobs import JobFactory, JobType
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.types import Channel
from jiuwen_memory.storage.kv import KVStore


@dataclass
class BackgroundDependencies:
    """从同一 Engine 取到的任务工厂、调度器、Evolver 和真源。"""

    factory: JobFactory | None
    scheduler: Scheduler
    evolver: Evolver | None
    kv: KVStore


async def start_hierarchy_derivation(
    scope: Scope, policy: PolicyManager, dependencies: BackgroundDependencies,
) -> list[str]:
    """默认关闭；开启后仅在实际支持周期的 Scheduler 注册一个 home。"""
    scope_contains(scope, scope)
    try:
        enabled = all(str(policy.get(key)).strip().lower() == "true"
                      for key in ("hierarchy.enabled", "hierarchy.auto_derive"))
    except PolicyError:
        enabled = False
    if not enabled:
        return []
    if dependencies.factory is None or dependencies.evolver is None:
        raise ValidationError("periodic hierarchy requires runtime JobFactory and Evolver")
    if dependencies.evolver.hierarchy_profile(HierarchyKind.TIME) is None:
        return []
    if not dependencies.scheduler.supports_periodic():
        raise ValidationError("hierarchy requires a periodic Scheduler and a live event loop")
    job = dependencies.factory.get_job(
        JobType.HIERARCHY_DERIVE, deepcopy(scope), evolver=dependencies.evolver,
        kv=dependencies.kv, policy=policy,
    )
    dependencies.scheduler.validate(job)
    return [await dependencies.scheduler.submit(job, Channel.BACKGROUND)]
