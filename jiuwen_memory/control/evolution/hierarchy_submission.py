# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式建树的策略分流；跳过任务仍由同一个 Scheduler 生成可查询身份。"""

from copy import deepcopy
from dataclasses import dataclass

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.construction.evolver import EvolveMode, Evolver
from jiuwen_memory.control.evolution.validation import (
    validate_evolve_options,
    validate_evolve_request,
)
from jiuwen_memory.control.jobs import Job, JobFactory, JobType
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.scheduler import Scheduler
from jiuwen_memory.control.types import EvolveTaskOptions, JobInfo, JobStatus
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore


class SkippedHierarchyJob(Job):
    """无构建依赖的成功空操作，不读取记忆、不修改树、不调用模型。"""

    @property
    def mode(self) -> str:
        return EvolveMode.HIERARCHY.value

    async def run(self) -> JobInfo:
        return JobInfo(
            scope=deepcopy(self.scope), mode=self.mode, status=JobStatus.SUCCEEDED,
            detail={
                "mode": self.mode, "complete": "true", "skipped": "true",
                "reason": "hierarchy_disabled", "built": "false",
                "created_parent_count": "0", "updated_child_count": "0",
                "replaced_parent_count": "0", "repair_required_count": "0",
            },
        )


def skipped_hierarchy_job(
    scope: Scope, options: EvolveTaskOptions, policy: PolicyManager | None,
) -> SkippedHierarchyJob | None:
    """调用者先校验基础请求；关闭分支先于树参数及构建依赖的校验。"""
    if options.mode is not EvolveMode.HIERARCHY:
        return None
    if policy is not None and (
        str(policy.all().get("hierarchy.enabled", "false")).strip().lower() == "true"
    ):
        return None
    return SkippedHierarchyJob(scope=deepcopy(scope))


@dataclass(frozen=True)
class HierarchySubmissionDependencies:
    """来自本 Engine 的共享依赖，避免任务另行装配读写源。"""

    policy: PolicyManager | None
    scheduler: Scheduler
    job_factory: JobFactory | None
    evolver: Evolver | None
    store: KVStore | DomainStore


async def submit_hierarchy(
    scope: Scope, options: EvolveTaskOptions, dependencies: HierarchySubmissionDependencies,
) -> str:
    """显式建树在普通演进链旁分流，关闭时不读取候选或解析构建依赖。"""
    scope, options = deepcopy(scope), deepcopy(options)
    validate_evolve_request(scope, options)
    if options.mode is not EvolveMode.HIERARCHY:
        raise ValidationError("建树提交只接受 HIERARCHY 模式")
    skipped = skipped_hierarchy_job(scope, options, dependencies.policy)
    if skipped is not None:
        return await dependencies.scheduler.submit(skipped, options.channel)
    validate_evolve_options(scope, options)
    if dependencies.job_factory is None or dependencies.evolver is None:
        raise ValidationError("建树需要配置 JobFactory 与 Evolver")
    job = dependencies.job_factory.get_job(
        JobType.HIERARCHY, scope=scope, options=options.hierarchy_options,
        reuse_existing=options.reuse_existing, kv=dependencies.store, evolver=dependencies.evolver,
    )
    return await dependencies.scheduler.submit(job, options.channel)
