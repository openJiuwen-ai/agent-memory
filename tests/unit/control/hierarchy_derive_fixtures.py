# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""周期建树的真实依赖和可推进时钟；断言仅放在测试函数。"""

from dataclasses import dataclass
from datetime import datetime

from jiuwen_memory.control.jobs_impl.hierarchy_derive_job import (
    HierarchyDeriveDependencies,
    HierarchyDeriveJob,
    HierarchyDeriveOptions,
)
from jiuwen_memory.control.jobs_impl.hierarchy_job import HierarchyJobDependencies
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from tests.unit.control.hierarchy_job_fixtures import HierarchyJobHarness


@dataclass
class DeriveClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def derive_job(harness: HierarchyJobHarness, clock: DeriveClock) -> HierarchyDeriveJob:
    """固定同源读写与显式 home，测试一轮不依赖真实时间。"""
    return HierarchyDeriveJob(harness.home, HierarchyDeriveDependencies(
        HierarchyJobDependencies(harness.kv, harness.evolver), DictPolicyManager({
            "hierarchy.enabled": "true", "hierarchy.auto_derive": "true",
        }), clock,
    ), HierarchyDeriveOptions(interval=1))
