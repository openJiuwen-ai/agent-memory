# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""一次性显式建树任务：锁覆盖取数与构建，任务结果忠实反映部分失败。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Callable, TypeVar

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.lock import LockHandle, LockLostError, LockProducer, LockProvider
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.construction.evolver import EvolveMode, Evolver, EvolveRequest
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeResult,
)
from jiuwen_memory.control.evolution.validation import validate_hierarchy_options
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import (
    HierarchyJobLimits,
    collect_candidates,
)
from jiuwen_memory.control.types import JobInfo, JobStatus
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.store_manager import StoreManagerProducer, resolve_name

ResultType = TypeVar("ResultType")


@dataclass(frozen=True)
class HierarchyJobDependencies:
    """运行时注入 Engine 同源的读端口与 Evolver，以及可选共享锁。"""

    kv: KVStore
    evolver: Evolver
    lock: LockProvider | None = None


class HierarchyJob(Job):
    """显式两/三/四层重建；不会读取消息、扩展权限或定时自动建树。"""

    def __init__(
        self, scope: Scope, dependencies: HierarchyJobDependencies,
        options: HierarchyComposeOptions, limits: HierarchyJobLimits | None = None,
    ) -> None:
        """捕获独立请求快照，提交后修改调用方对象不影响执行。"""
        captured_scope = deepcopy(scope)
        captured_options = deepcopy(options)
        validate_hierarchy_options(captured_scope, captured_options)
        if dependencies.evolver is None:
            raise ValidationError("HierarchyJob requires the Engine Evolver at runtime")
        super().__init__(scope=captured_scope, interval=0)
        self._request_scope = deepcopy(captured_scope)
        self._dependencies = dependencies
        self._options = captured_options
        self._mode = EvolveMode.HIERARCHY
        self._limits = limits or HierarchyJobLimits()

    @property
    def mode(self) -> str:
        """供鉴权和任务查询识别显式建树。"""
        return self._mode.value

    async def run(self) -> JobInfo:
        """失败不伪装成功，失锁也不声称已回滚完成的写入。"""
        detail = self._initial_detail()
        try:
            validate_hierarchy_options(self._request_scope, self._options)
            if self.scope != self._request_scope:
                raise ValidationError("HierarchyJob Scope changed after submission")
            lock_provider = self._dependencies.lock
            if lock_provider is None:
                return await self._run_inner(detail, None)
            async with lock_provider.guard(
                self._request_scope, f"hierarchy:{self._options.kind.value}",
                wait_timeout_ms=self._limits.lock_wait_ms,
            ) as handle:
                return await self._run_inner(detail, handle)
        except Exception as error:
            detail.update(complete="false", error=f"{type(error).__name__}: {error}")
            return self._info(JobStatus.FAILED, detail)

    async def _run_inner(self, detail: dict[str, str], handle: LockHandle | None) -> JobInfo:
        _ensure_lock(handle)
        candidates = await blocking_call(
            collect_candidates,
            (self._dependencies.kv, self._request_scope, self._options, self._limits),
        )
        _ensure_lock(handle)
        if not candidates.leaves:
            detail.update(complete="true", reason="no candidates")
            return self._info(JobStatus.SUCCEEDED, detail)
        request = EvolveRequest(
            units=[*candidates.leaves.values(), *candidates.parents.values()],
            mode=EvolveMode.HIERARCHY,
            metadata={"trigger": "explicit"},
            hierarchy_options=deepcopy(self._options),
        )
        evolved = await blocking_call(self._dependencies.evolver.evolve, (request,))
        composition = evolved.hierarchy_result
        if not isinstance(composition, HierarchyComposeResult):
            raise ValidationError("Hierarchy Evolver did not return hierarchy_result")
        _record_result(detail, composition)
        _ensure_lock(handle)
        if not composition.complete or composition.repair_required:
            detail.update(
                complete="false", error="hierarchy composition incomplete; repair required",
            )
            return self._info(JobStatus.FAILED, detail)
        return self._info(JobStatus.SUCCEEDED, detail)

    def _initial_detail(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "kind": self._options.kind.value,
            "span_start": self._options.span_start.isoformat(),
            "span_end": self._options.span_end.isoformat(),
            "trigger": "explicit",
            "created_parent_count": "0",
            "updated_child_count": "0",
            "replaced_parent_count": "0",
            "repair_required_count": "0",
            "complete": "false",
        }

    def _info(self, status: JobStatus, detail: dict[str, str]) -> JobInfo:
        return JobInfo(
            scope=deepcopy(self._request_scope), mode=self.mode, status=status, detail=detail,
        )


def _ensure_lock(handle: LockHandle | None) -> None:
    if handle is not None and handle.lost.is_set():
        raise LockLostError("hierarchy lock lost; completed writes are not rolled back")


async def blocking_call(function: Callable[..., ResultType], arguments: tuple) -> ResultType:
    """取消时仍等待同步线程结束，防止持有的层级锁提前释放。"""
    # Cancelled to_thread work keeps running. Wait before releasing the surrounding lock.
    worker = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        finally:
            raise


def _record_result(detail: dict[str, str], result: HierarchyComposeResult) -> None:
    detail.update(
        created_parent_count=str(len(result.created_parent_ids)),
        updated_child_count=str(len(result.updated_child_ids)),
        replaced_parent_count=str(len(result.replaced_parent_ids)),
        repair_required_count=str(len(result.repair_required)),
        complete=str(result.complete).lower(),
        repair_required=json.dumps([asdict(repair) for repair in result.repair_required]),
    )


@dataclass
class HierarchyJobSpec:
    """装配期不解析 Evolver；运行时 KV 覆盖可保证 Engine 读写同源。"""

    kv: KVStore
    limits: HierarchyJobLimits = field(default_factory=HierarchyJobLimits)
    lock: LockProvider | None = None

    def with_scope(self, scope: Scope, **kwargs) -> HierarchyJob:
        """运行时必须提供 options 与 Evolver，不允许隐式默认或周期参数。"""
        runtime_evolver = kwargs.pop("evolver", None)
        runtime_options = kwargs.pop("options", None)
        runtime_kv = kwargs.pop("kv", None)
        if kwargs:
            raise ValidationError(f"unknown HierarchyJob arguments: {sorted(kwargs)}")
        if runtime_evolver is None:
            raise ValidationError("HierarchyJob requires the Engine Evolver at runtime")
        return HierarchyJob(
            scope=scope,
            dependencies=HierarchyJobDependencies(
                self.kv if runtime_kv is None else runtime_kv, runtime_evolver, self.lock,
            ),
            options=runtime_options,
            limits=self.limits,
        )


def build_spec(config) -> HierarchyJobSpec:
    """只解析真源、读取保护配置和显式锁引用，不解析构建算子。"""
    lock_provider = None
    if config.params.get("lock") is not None:
        lock_provider = LockProducer.dep(config)
    return HierarchyJobSpec(
        kv=StoreManagerProducer.resolve(config).kv(resolve_name(config, "kv_store")),
        limits=HierarchyJobLimits(
            max_leaves=config.get("hierarchy_max_leaves", 5000),
            page_size=config.get("hierarchy_page_size", 200),
            lock_wait_ms=config.get("hierarchy_lock_wait_ms", 30_000),
        ),
        lock=lock_provider,
    )
