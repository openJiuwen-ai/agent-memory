# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""指定 home 上的周期增量建树；周期由 Scheduler 承担，不接管宿主事件循环。

逐层立即交付，未封口下层的时间边界阻挡上层；一层失败即停止该 home。部分写入
在本次注册共享的状态中闩住，后续轮不自动修复或推进；重启前须人工处理 repair。
只使用 Evolver 绑定 Composer 的 profile；不全库发现 home、不清空 agent/user 维度。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from jiuwen_memory.common.errors import PolicyError, ValidationError
from jiuwen_memory.common.lock import LockHandle, LockLostError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope
from jiuwen_memory.construction.evolver import EvolveMode, EvolveRequest
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
    HierarchyComposeResult,
    HierarchyIncrementalContext,
    validate_time_parent_roles,
)
from jiuwen_memory.control.evolution.validation import scope_contains
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import HierarchyJobLimits, scope_key
from jiuwen_memory.control.jobs_impl.hierarchy_incremental import collect_incremental, utc
from jiuwen_memory.control.jobs_impl.hierarchy_job import (
    HierarchyJobDependencies,
    HierarchyJobSpec,
    blocking_call,
    build_spec,
)
from jiuwen_memory.control.policy import PolicyManager
from jiuwen_memory.control.types import JobInfo, JobStatus


@dataclass(frozen=True)
class HierarchyDeriveOptions:
    """周期和单轮扫描窗口，算法和静默阈值仍来自 Composer profile。"""

    interval: int = 1800
    lookback_seconds: int = 604800

    def __post_init__(self) -> None:
        for value in (self.interval, self.lookback_seconds):
            if type(value) is not int or value <= 0:
                raise ValidationError("derive interval/lookback must be positive integers")


@dataclass
class HierarchyDeriveState:
    """周期副本共享的故障闸；不是持久化检查点或自动修复器。"""

    needs_repair: bool = False
    failure_detail: dict[str, str] = field(default_factory=dict)


@dataclass
class HierarchyDeriveDependencies:
    """同源读写依赖、运行时策略与可注入时钟。"""

    hierarchy: HierarchyJobDependencies
    policy: PolicyManager
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


class HierarchyDeriveJob(Job):
    """一轮逐层增量，公开 scope 固定为实际 tree home。"""

    def __init__(
        self, scope: Scope, dependencies: HierarchyDeriveDependencies,
        options: HierarchyDeriveOptions | None = None, limits: HierarchyJobLimits | None = None,
    ) -> None:
        """快照 scope/options；任务本身不创建周期循环。"""
        scope_contains(scope, scope)
        settings = options or HierarchyDeriveOptions()
        super().__init__(deepcopy(scope), settings.interval)
        self.dependencies = dependencies
        self.options = settings
        self.limits = limits or HierarchyJobLimits()
        self.state = HierarchyDeriveState()
        self._home = deepcopy(scope)
        self._mode = EvolveMode.HIERARCHY

    @property
    def mode(self) -> str:
        """复用 HIERARCHY 任务状态鉴权。"""
        return self._mode.value

    async def run(self) -> JobInfo:
        """实时策略闸门、整轮共享锁、终态与错误计数。"""
        detail = {"trigger": "auto_derive", "created_parent_count": "0",
                  "updated_child_count": "0", "deferred_child_count": "0",
                  "needs_rebuild_count": "0", "repair_required_count": "0",
                  "complete": "false"}
        try:
            if self.scope != self._home:
                raise ValidationError("derive Scope changed after registration")
            if not self._enabled():
                detail.update(complete="true", reason="auto_derive disabled")
                return self._info(JobStatus.SUCCEEDED, detail)
            if self.state.needs_repair:
                if self.state.failure_detail:
                    detail.update(self.state.failure_detail)
                    detail["paused_reason"] = "repair before runtime restart"
                    return self._info(JobStatus.FAILED, detail)
                raise ValidationError(
                    "previous composition incomplete; repair before runtime restart"
                )
            profile = self.dependencies.hierarchy.evolver.hierarchy_profile(HierarchyKind.TIME)
            if profile is None:
                detail.update(complete="true", reason="no compose profile")
                return self._info(JobStatus.SUCCEEDED, detail)
            _validate_profile(profile)
            lock = self.dependencies.hierarchy.lock
            if lock is None:
                return await self._derive(profile, detail, None)
            async with lock.guard(self._home, "hierarchy:time",
                                  wait_timeout_ms=self.limits.lock_wait_ms) as handle:
                return await self._derive(profile, detail, handle)
        except Exception as error:
            detail.update(complete="false", error=f"{type(error).__name__}: {error}")
            if self.state.needs_repair and not self.state.failure_detail:
                self.state.failure_detail = deepcopy(detail)
            return self._info(JobStatus.FAILED, detail)

    async def _derive(
        self, profile: HierarchyComposeProfile, detail: dict[str, str], handle: LockHandle | None,
    ) -> JobInfo:
        # 此处保持现有取消时等待 to_thread 结束的规则；锁覆盖整个 home 的各层。
        now = utc(self.dependencies.clock())
        frontier = None
        chain = (profile.leaf_role, *profile.parent_roles)
        for leaf_role, parent_role in zip(chain, chain[1:]):
            _check_lock(handle)
            options = HierarchyComposeOptions(
                kind=HierarchyKind.TIME, leaf_role=leaf_role, parent_roles=[parent_role],
                tree_home_scope=deepcopy(self._home), span_start=now - timedelta(
                    seconds=self.options.lookback_seconds), span_end=now,
            )
            selected = await blocking_call(collect_incremental, (
                self.dependencies.hierarchy.kv, options, profile.parent_roles, self.limits,
            ))
            _check_lock(handle)
            if selected.needs_rebuild_count:
                detail["needs_rebuild_count"] = str(selected.needs_rebuild_count)
                raise ValidationError("late or outside-window input requires explicit rebuild")
            frontier = _earlier(frontier, selected.pending_before)
            if not selected.inputs:
                continue
            request = EvolveRequest(
                units=selected.inputs, mode=EvolveMode.HIERARCHY, hierarchy_options=options,
                metadata={"trigger": "auto_derive"},
                hierarchy_incremental=HierarchyIncrementalContext(
                    settle_at=now, ready_before=frontier,
                    supporting_units=selected.supporting_units,
                ),
            )
            self.state.needs_repair = True
            evolved = await blocking_call(self.dependencies.hierarchy.evolver.evolve, (request,))
            result = evolved.hierarchy_result
            if not isinstance(result, HierarchyComposeResult):
                raise ValidationError("incremental Evolver did not return hierarchy_result")
            _add_counts(detail, result)
            _check_lock(handle)
            if not result.complete or result.repair_required:
                raise ValidationError("incremental composition incomplete; repair required")
            self.state.needs_repair = False
            frontier = _earlier(frontier, result.pending_before)
        detail.update(complete="true", pending_before=frontier.isoformat() if frontier else "")
        return self._info(JobStatus.SUCCEEDED, detail)

    def _enabled(self) -> bool:
        try:
            return all(str(self.dependencies.policy.get(key)).strip().lower() == "true"
                       for key in ("hierarchy.enabled", "hierarchy.auto_derive"))
        except PolicyError:
            return False

    def _info(self, status: JobStatus, detail: dict[str, str]) -> JobInfo:
        return JobInfo(scope=deepcopy(self._home), mode=self.mode, status=status, detail=detail)


def _earlier(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    return left if right is None else min(left, right)


def _check_lock(handle: LockHandle | None) -> None:
    if handle is not None and handle.lost.is_set():
        raise LockLostError("hierarchy lock lost; completed writes are not rolled back")


def _add_counts(detail: dict[str, str], result: HierarchyComposeResult) -> None:
    for key, count in (("created_parent_count", len(result.created_parent_ids)),
                       ("updated_child_count", len(result.updated_child_ids)),
                       ("deferred_child_count", result.deferred_child_count)):
        detail[key] = str(int(detail[key]) + count)
    detail["repair_required_count"] = str(len(result.repair_required))
    detail["repair_required"] = json.dumps([asdict(repair) for repair in result.repair_required])


def _validate_profile(profile: HierarchyComposeProfile) -> None:
    if not isinstance(profile, HierarchyComposeProfile) or profile.kind is not HierarchyKind.TIME:
        raise ValidationError("derive requires a TIME compose profile")
    if profile.leaf_role is not HierarchyRole.SNAPSHOT or not isinstance(
        profile.parent_roles, tuple,
    ):
        raise ValidationError("derive profile requires snapshot and an ordered parent tuple")
    validate_time_parent_roles(list(profile.parent_roles))


@dataclass
class HierarchyDeriveJobSpec:
    """只固定调度和读取配置；Evolver/KV/Policy 由启动时注入。"""

    hierarchy: HierarchyJobSpec
    options: HierarchyDeriveOptions = field(default_factory=HierarchyDeriveOptions)
    states: dict[tuple, HierarchyDeriveState] = field(default_factory=dict)

    def with_scope(self, scope: Scope, **kwargs) -> HierarchyDeriveJob:
        """强制使用运行时同源依赖，不偷偷解析默认模型或清空 Scope。"""
        if set(kwargs) != {"evolver", "kv", "policy"} or any(
            value is None for value in kwargs.values()
        ):
            raise ValidationError("derive requires runtime evolver, kv and policy")
        job = HierarchyDeriveJob(
            scope, HierarchyDeriveDependencies(HierarchyJobDependencies(
                kwargs["kv"], kwargs["evolver"], self.hierarchy.lock,
            ), kwargs["policy"]), self.options, self.hierarchy.limits,
        )
        job.state = self.states.setdefault(scope_key(scope), HierarchyDeriveState())
        return job


def build_derive_spec(config) -> HierarchyDeriveJobSpec:
    """装配增量任务模板，不解析内容算子。"""
    return HierarchyDeriveJobSpec(build_spec(config), HierarchyDeriveOptions(
        interval=config.get("hierarchy_derive_interval", 1800),
        lookback_seconds=config.get("hierarchy_derive_lookback", 604800),
    ))
