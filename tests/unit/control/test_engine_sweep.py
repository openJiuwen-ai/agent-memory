"""Engine.sweep_expired 编排契约测试（C-03）。

核心语义（见 ``sweep_support.run_sweep`` 与 ``MemoryEngine.sweep_expired``）：

1. ``LifecycleManager.sweep()`` 纯计算返回 transition，不写真源；
2. FORGOTTEN 组**先** ``IndexBuilder.remove(mode=SOFT)``、成功后回写真源——
   顺序不变量保证 remove 失败时单元保持 ACTIVE，下轮 sweep 重新选中自愈；
3. ARCHIVED 组只回写、不动检索索引（``include_archived`` 召回与 ``as_of``
   回溯仍需索引）；
4. 任一步失败的组计入 ``failed``、真源保持原状态，不静默当成功；
5. 重试幂等：remove 失败后下轮重跑，remove 重做（幂等）、回写补齐。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Scope, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.engine_impl.sweep_support import run_sweep
from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition
from jiuwen_memory.control.lifecycle_impl.kv_lifecycle_manager import KVLifecycleManager
from jiuwen_memory.control.policy_impl.dict_policy_manager import DictPolicyManager
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.storage_impl.composite_storage import CompositeStorage
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

pytestmark = pytest.mark.unit


class _Call:
    """全局调用序列的一条记录（验证 remove 先于 transition 的顺序不变量）。"""

    def __init__(self, op: str, unit_ids: list[str], mode: str | None = None) -> None:
        self.op = op
        self.unit_ids = sorted(unit_ids)
        self.mode = mode


class _ScriptedLifecycle(LifecycleManager):
    """可注入 transitions / 可注入 transition 失败的 LifecycleManager 替身。

    ``journal``：与 remove 替身共享的调用日志（记录全局时序，验证顺序不变量）。
    """

    def __init__(
        self,
        transitions: list[SweepTransition],
        *,
        fail_transition_for: set[str] | None = None,
        journal: list[_Call] | None = None,
    ) -> None:
        self._transitions = list(transitions)
        self._fail_transition_for = fail_transition_for or set()
        self.calls: list[_Call] = journal if journal is not None else []

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope: Scope, unit_ids: list[str], target: LifecycleState) -> None:
        self.calls.append(_Call("transition", unit_ids))
        if self._fail_transition_for & set(unit_ids):
            raise RuntimeError("mock transition failure")

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        raise AssertionError("run_sweep must not call supersede")

    def sweep(self) -> list[SweepTransition]:
        return list(self._transitions)


class _ScriptedIndex:
    """remove 替身：记录 (units, mode)，可按轮次注入失败。"""

    def __init__(
        self,
        *,
        fail_for: set[str] | None = None,
        fail_calls: int = 0,
        journal: list[_Call] | None = None,
    ) -> None:
        self._fail_for = fail_for or set()
        self._fail_calls = fail_calls
        self._call_count = 0
        self.calls: list[_Call] = journal if journal is not None else []

    def __call__(self, units: list[MemoryUnit]) -> None:
        self._call_count += 1
        self.calls.append(_Call("remove", [u.id for u in units], IndexRemoveMode.SOFT.value))
        if self._fail_for & {u.id for u in units} and self._call_count <= self._fail_calls:
            raise RuntimeError("mock remove failure")


def _transition(unit: MemoryUnit, to_state: LifecycleState) -> SweepTransition:
    return SweepTransition(
        scope=unit.scope,
        unit_id=unit.id,
        from_state=unit.lifecycle,
        to_state=to_state,
        unit=unit,
    )


# ---- A. run_sweep 编排语义 ----


def test_run_sweep_removes_forgotten_from_index_before_transition(unit_factory) -> None:
    expired = unit_factory("expired", "expired active")
    transition = _transition(expired, LifecycleState.FORGOTTEN)
    journal: list[_Call] = []
    lifecycle = _ScriptedLifecycle([transition], journal=journal)
    remove = _ScriptedIndex(journal=journal)

    result = run_sweep([transition], lifecycle, remove)

    assert result.swept == ["expired"]
    assert result.failed == []
    # 顺序不变量：remove(SOFT) 先于真源回写——remove 失败时单元仍 ACTIVE，
    # 下轮 sweep 重新选中；反序会留下真源已 FORGOTTEN、索引未清的脏条目。
    assert [(c.op, c.unit_ids) for c in journal] == [
        ("remove", ["expired"]),
        ("transition", ["expired"]),
    ]
    assert journal[0].mode == IndexRemoveMode.SOFT.value


def test_run_sweep_keeps_archived_units_in_index(unit_factory) -> None:
    expired = unit_factory("expired", "expired active")
    transition = _transition(expired, LifecycleState.ARCHIVED)
    lifecycle = _ScriptedLifecycle([transition])
    remove = _ScriptedIndex()

    result = run_sweep([transition], lifecycle, remove)

    assert result.swept == ["expired"]
    # ARCHIVED 只回写真源：include_archived 召回与 as_of 回溯仍需检索索引。
    assert remove.calls == []
    assert [c.op for c in lifecycle.calls] == ["transition"]


def test_run_sweep_remove_failure_keeps_unit_for_retry(unit_factory) -> None:
    expired = unit_factory("expired", "expired active")
    transition = _transition(expired, LifecycleState.FORGOTTEN)
    lifecycle = _ScriptedLifecycle([transition])
    remove = _ScriptedIndex(fail_for={"expired"}, fail_calls=1)

    result = run_sweep([transition], lifecycle, remove)

    # remove 失败：不回写真源、计入 failed——单元保持 ACTIVE，下轮 sweep 重新发现。
    assert result.swept == []
    assert result.failed == ["expired"]
    assert lifecycle.calls == []


def test_run_sweep_retry_after_remove_failure_heals(unit_factory) -> None:
    expired = unit_factory("expired", "expired active")
    transition = _transition(expired, LifecycleState.FORGOTTEN)
    lifecycle = _ScriptedLifecycle([transition])
    remove = _ScriptedIndex(fail_for={"expired"}, fail_calls=1)

    first = run_sweep(lifecycle.sweep(), lifecycle, remove)
    second = run_sweep(lifecycle.sweep(), lifecycle, remove)

    # 下轮重试：remove 幂等重做、回写补齐——失败不静默、可自愈。
    assert first.failed == ["expired"]
    assert second.swept == ["expired"]
    assert second.failed == []
    assert [c.op for c in lifecycle.calls] == ["transition"]


def test_run_sweep_transition_failure_keeps_unit_for_retry(unit_factory) -> None:
    expired = unit_factory("expired", "expired active")
    transition = _transition(expired, LifecycleState.FORGOTTEN)
    lifecycle = _ScriptedLifecycle([transition], fail_transition_for={"expired"})
    remove = _ScriptedIndex()

    result = run_sweep([transition], lifecycle, remove)

    # remove 成功但回写失败：单元仍 ACTIVE，下轮 sweep 重新发现（remove 幂等
    # 重做无害），计入 failed 不静默当成功。
    assert result.swept == []
    assert result.failed == ["expired"]
    assert [c.op for c in remove.calls] == ["remove"]


def test_run_sweep_groups_by_scope_and_target(unit_factory) -> None:
    scope_a = Scope(org="acme", space="space-a", user="alice")
    scope_b = Scope(org="acme", space="space-b", user="alice")
    forget_a = unit_factory("forget-a", "expired in space A", scope=scope_a)
    archive_a = unit_factory("archive-a", "archived in space A", scope=scope_a)
    forget_b = unit_factory("forget-b", "expired in space B", scope=scope_b)
    transitions = [
        _transition(forget_a, LifecycleState.FORGOTTEN),
        _transition(archive_a, LifecycleState.ARCHIVED),
        _transition(forget_b, LifecycleState.FORGOTTEN),
    ]
    journal: list[_Call] = []
    lifecycle = _ScriptedLifecycle(transitions, journal=journal)
    remove = _ScriptedIndex(journal=journal)

    result = run_sweep(transitions, lifecycle, remove)

    assert result.swept == ["archive-a", "forget-a", "forget-b"]
    assert result.failed == []
    # 按 (scope, 目标态) 分组：FORGOTTEN 两组各 remove+transition，ARCHIVED 只 transition。
    removes = [c for c in journal if c.op == "remove"]
    transitions_seen = [c for c in journal if c.op == "transition"]
    assert sorted(c.unit_ids for c in removes) == [["forget-a"], ["forget-b"]]
    assert sorted(c.unit_ids for c in transitions_seen) == [
        ["archive-a"],
        ["forget-a"],
        ["forget-b"],
    ]


# ---- B. InMemoryEngine.sweep_expired 集成（真 KVLifecycleManager） ----


class _EngineSpyIndex(IndexBuilder):
    """Engine 侧 IndexBuilder 替身：记录 remove 收到的 (units, mode)。"""

    def __init__(self) -> None:
        self.removed: list[tuple[list[str], IndexRemoveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        return None

    def update(self, units, *, mode: IndexWriteMode = IndexWriteMode.ALL) -> None:
        return None

    def remove(self, units, *, mode: IndexRemoveMode = IndexRemoveMode.HARD) -> None:
        self.removed.append(([u.id for u in units], mode))

    def rebuild(self) -> None:
        return None


def _engine(kv: InMemoryKVStore, lifecycle: KVLifecycleManager) -> InMemoryEngine:
    storage = CompositeStorage(kv=kv)
    return InMemoryEngine(
        ingestor=None,  # sweep 路径不依赖 ingestor
        index_builder=_EngineSpyIndex(),
        retriever=None,  # sweep 路径不依赖 retriever
        storage=storage,
        scheduler=InProcessScheduler(),
        evolver=None,  # sweep 路径不依赖 evolver
        lifecycle=lifecycle,
    )


def test_engine_sweep_expired_forgets_and_cleans_derived_index(unit_factory) -> None:
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=datetime.now(timezone.utc) - timedelta(days=1),
    )
    superseded = unit_factory("superseded", "old version", lifecycle=LifecycleState.SUPERSEDED)
    keep = unit_factory("keep", "still active", lifecycle=LifecycleState.ACTIVE)
    scope = expired.scope
    kv = InMemoryKVStore()
    for unit in [expired, superseded, keep]:
        kv.insert(unit.scope, memory_key(unit.id), dumps(unit))
    lifecycle = KVLifecycleManager(CompositeStorage(kv=kv))
    engine = _engine(kv, lifecycle)

    result = asyncio.run(engine.sweep_expired())
    spy = engine._index

    assert result.swept == [expired.id, superseded.id]
    assert result.failed == []
    # FORGOTTEN：派生索引移出（SOFT）+ 真源回写；未到期单元不动。
    assert spy.removed == [([expired.id, superseded.id], IndexRemoveMode.SOFT)]
    assert loads(kv.get(scope, memory_key(expired.id))).lifecycle == LifecycleState.FORGOTTEN
    assert loads(kv.get(scope, memory_key(superseded.id))).lifecycle == LifecycleState.FORGOTTEN
    assert loads(kv.get(scope, memory_key(keep.id))).lifecycle == LifecycleState.ACTIVE


def test_engine_sweep_expired_archived_policy_keeps_derived_index(unit_factory) -> None:
    expired = unit_factory(
        "expired",
        "expired active",
        lifecycle=LifecycleState.ACTIVE,
        t_invalid=datetime.now(timezone.utc) - timedelta(days=1),
    )
    scope = expired.scope
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key(expired.id), dumps(expired))
    lifecycle = KVLifecycleManager(
        CompositeStorage(kv=kv),
        DictPolicyManager({"lifecycle.expired_active.target": "archived"}),
    )
    engine = _engine(kv, lifecycle)

    result = asyncio.run(engine.sweep_expired())
    spy = engine._index

    # ARCHIVED：只回写真源，检索索引保留（include_archived / as_of 回溯仍需）。
    assert result.swept == [expired.id]
    assert result.failed == []
    assert spy.removed == []
    assert loads(kv.get(scope, memory_key(expired.id))).lifecycle == LifecycleState.ARCHIVED
