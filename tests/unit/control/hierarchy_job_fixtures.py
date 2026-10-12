# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HierarchyJob 测试的公开装配与可观测依赖，不访问生产 protected 成员。"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from jiuwen_memory.common.lock import LockHandle, LockProvider
from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
from jiuwen_memory.common.type_def import HierarchyKind, MemoryUnit, Scope, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.evolver import Evolver, EvolveRequest, EvolveResult
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeProfile,
)
from jiuwen_memory.construction.hierarchy_composer_impl import DefaultHierarchyComposer
from jiuwen_memory.construction.index_builder_impl.hybrid_index_builder import HybridIndexBuilder
from jiuwen_memory.control.jobs_impl.hierarchy_job import (
    HierarchyJob,
    HierarchyJobDependencies,
    HierarchyJobLimits,
)
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.store_manager_impl import CompositeStoreManager
from tests.unit.construction.fixtures import create_test_plugins, create_test_stores
from tests.unit.construction.hierarchy_evolve_fixtures import make_evolver_harness
from tests.unit.construction.hierarchy_fixtures import (
    ORIGIN,
    CompositionHarness,
    RecordingIndexBuilder,
    make_leaf,
    make_request,
)


class ObservedEvolver(Evolver):
    """记录真实演进的输入，允许公开注入错误和返回值。"""

    def __init__(self, delegate: Evolver | None = None) -> None:
        self.delegate = delegate
        self.requests: list[EvolveRequest] = []
        self.response = EvolveResult()
        self.before_evolve: Callable[[], None] | None = None

    @staticmethod
    def operator_type() -> OperatorType:
        return OperatorType.EVOLVER

    @staticmethod
    def health() -> None:
        return None

    def evolve(self, request: EvolveRequest) -> EvolveResult:
        self.requests.append(deepcopy(request))
        if self.before_evolve is not None:
            self.before_evolve()
        if self.delegate is not None:
            return self.delegate.evolve(request)
        return self.response

    def hierarchy_profile(self, kind: HierarchyKind) -> HierarchyComposeProfile | None:
        """保持真实 Composer 配置来源，未设 delegate 时不宣称增量能力。"""
        return self.delegate.hierarchy_profile(kind) if self.delegate is not None else None


class ObservedLock(InMemoryLockProvider):
    """用公开 acquire 凭据观察锁持有和模拟租约失效。"""

    def __init__(self) -> None:
        super().__init__(wait_timeout_ms=0)
        self.handle: LockHandle | None = None
        self.lose_at_acquire = False

    async def acquire(self, scope: Scope, name: str, **kwargs) -> LockHandle:
        self.handle = await super().acquire(scope, name, **kwargs)
        if self.lose_at_acquire:
            self.handle.lost.set()
        return self.handle

    def lose(self) -> None:
        if self.handle is None:
            raise RuntimeError("test lock has not been acquired")
        self.handle.lost.set()


@dataclass
class HierarchyJobHarness:
    composition: CompositionHarness
    evolver: ObservedEvolver
    options: HierarchyComposeOptions

    @property
    def kv(self) -> KVStore:
        return self.composition.manager.kv()

    @property
    def home(self) -> Scope:
        return self.options.tree_home_scope

    def job(
        self, limits: HierarchyJobLimits | None = None, lock: LockProvider | None = None,
    ) -> HierarchyJob:
        """始终注入与真实 Composer 同源的真源和 Evolver。"""
        return HierarchyJob(
            self.home, HierarchyJobDependencies(self.kv, self.evolver, lock),
            self.options, limits,
        )

    def read(self, original: MemoryUnit) -> MemoryUnit:
        return self.composition.read(original.scope, original.id)

    def overwrite(self, unit: MemoryUnit) -> None:
        self.kv.update(unit.scope, memory_key(unit.id), dumps(unit))


def job_harness(
    leaves: list[MemoryUnit] | None = None,
    *, profiles: dict[HierarchyKind, HierarchyComposeProfile] | None = None,
) -> HierarchyJobHarness:
    """真实内存 IndexBuilder + Composer + Evolver，默认一片叶、跨 session 的 home。"""
    starting_leaves = [make_leaf("one")] if leaves is None else leaves
    stores = create_test_stores()
    stores["kv"] = InMemoryKVStore()
    plugins = create_test_plugins()
    manager = CompositeStoreManager(**stores)
    builder = HybridIndexBuilder(manager, plugins["chunker"], plugins["embedder"])
    builder.build(starting_leaves)
    recorder = RecordingIndexBuilder(builder)
    composition = CompositionHarness(DefaultHierarchyComposer(recorder, profiles or {}),
                                     recorder, manager)
    original = make_request(
        starting_leaves or [make_leaf("unused")],
        span=(ORIGIN, ORIGIN + timedelta(hours=1)),
    )
    evolver = make_evolver_harness(composition, composition.composer).evolver
    return HierarchyJobHarness(composition, ObservedEvolver(evolver), original.options)
