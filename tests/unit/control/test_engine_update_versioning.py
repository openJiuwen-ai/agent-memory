from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jiuwen_memory.api import DeleteMode, DeleteSelector, MemoryPatch, Scope, UpdateMode
from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.errors import BackendError, ConflictError, ValidationError
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Temporal
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.engine_impl.update_support import (
    finalize_supersede_or_rollback,
    should_rollback_after_supersede_error,
)
from jiuwen_memory.control.lifecycle import LifecycleManager, SweepTransition
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from tests.unit.api.fixtures import security_for


class RecordingLifecycle(LifecycleManager):
    def __init__(self, kv) -> None:
        self._kv = kv
        self.supersede_calls: list[tuple[Scope, str, datetime]] = []

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(self, scope: Scope, unit_ids: list[str], target: LifecycleState) -> None:
        raise AssertionError("Engine.update(SUPERSEDE) should call supersede(), not transition()")

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        self.supersede_calls.append((scope, unit_id, invalid_at))
        for key, raw in self._kv.scan(scope):
            if key.rsplit("/", 1)[-1] == unit_id:
                unit = loads(raw)
                if unit.lifecycle != LifecycleState.ACTIVE:
                    raise ValidationError("invalid test transition")
                unit.lifecycle = LifecycleState.SUPERSEDED
                unit.temporal.t_invalid = invalid_at
                self._kv.update(scope, key, dumps(unit))
                return unit
        raise AssertionError(f"missing test unit: {unit_id}")

    def sweep(self) -> list[SweepTransition]:
        return []


def test_supersede_sets_version_chain_and_invalidates_old_version() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    old = kernel.api.add("home is Shanghai", scope, security=security_for(kernel.api, actor))[0]

    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing"),
        security=security_for(kernel.api, actor),
    )

    stored_old = kernel.api.get(old.id, scope, security=security_for(kernel.api, actor))
    assert new.id != old.id
    assert new.supersedes == old.id
    assert new.temporal.t_valid is not None
    assert stored_old.lifecycle == LifecycleState.SUPERSEDED
    assert stored_old.temporal.t_invalid == new.temporal.t_valid


def test_supersede_uses_patch_valid_time_as_new_version_boundary() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    valid_from = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)

    old = kernel.api.add("home is Shanghai", scope, security=security_for(kernel.api, actor))[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing", t_valid=valid_from),
        security=security_for(kernel.api, actor),
    )

    stored_old = kernel.api.get(old.id, scope, security=security_for(kernel.api, actor))
    assert new.temporal.t_valid == valid_from
    assert stored_old.temporal.t_invalid == valid_from


def test_update_supersede_delegates_old_version_lifecycle_to_manager() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    lifecycle = RecordingLifecycle(kv)
    setattr(getattr(kernel.api, "_engine"), "_lifecycle", lifecycle)
    valid_from = datetime(2026, 6, 17, 11, 0, tzinfo=UTC)

    old = kernel.api.add("home is Shanghai", scope, security=security_for(kernel.api, actor))[0]
    new = kernel.api.update(
        old.id,
        scope,
        MemoryPatch(content="home is Beijing", t_valid=valid_from),
        security=security_for(kernel.api, actor),
    )

    assert lifecycle.supersede_calls == [(scope, old.id, valid_from)]
    stored_old = kernel.api.get(old.id, scope, security=security_for(kernel.api, actor))
    assert stored_old.lifecycle == LifecycleState.SUPERSEDED
    assert stored_old.temporal.t_invalid == valid_from
    assert new.supersedes == old.id


@pytest.mark.parametrize("mode", [UpdateMode.SUPERSEDE, UpdateMode.OVERWRITE])
def test_update_rejects_superseded_unit_without_forking(mode: UpdateMode) -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    first = kernel.api.add("home is Shanghai", scope, security=security_for(kernel.api, actor))[0]
    second = kernel.api.update(
        first.id,
        scope,
        MemoryPatch(content="home is Beijing"),
        security=security_for(kernel.api, actor),
    )
    stored_first_before = kernel.api.get(first.id, scope, security=security_for(kernel.api, actor))

    with pytest.raises(ConflictError):
        kernel.api.update(
            first.id,
            scope,
            MemoryPatch(content="home is Shenzhen", mode=mode),
            security=security_for(kernel.api, actor),
        )

    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    active = [unit for unit in listed if unit.lifecycle == LifecycleState.ACTIVE]
    assert [unit.id for unit in active] == [second.id], (
        "对 superseded 记录再次 update 绝不能分叉出第二个 active 版本"
    )
    stored_first_after = kernel.api.get(first.id, scope, security=security_for(kernel.api, actor))
    assert stored_first_after.lifecycle == LifecycleState.SUPERSEDED
    assert stored_first_after.temporal.t_invalid == stored_first_before.temporal.t_invalid
    assert stored_first_after.content == "home is Shanghai"

    # 调用方收到 ConflictError 后，可通过 as_of=now 沿版本链拿到当前有效版本 B
    latest = kernel.api.get(
        first.id,
        scope,
        security=security_for(kernel.api, actor),
        as_of=datetime.now(UTC),
    )
    assert latest.id == second.id


@pytest.mark.parametrize("delete_mode", [DeleteMode.ARCHIVE, DeleteMode.FORGET])
@pytest.mark.parametrize("update_mode", [UpdateMode.SUPERSEDE, UpdateMode.OVERWRITE])
def test_update_rejects_archived_and_forgotten_without_orphan(
    delete_mode: DeleteMode, update_mode: UpdateMode
) -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    unit = kernel.api.add("retired fact", scope, security=security_for(kernel.api, actor))[0]
    kernel.api.delete(
        DeleteSelector(unit_ids=[unit.id], scope=scope, mode=delete_mode),
        security=security_for(kernel.api, actor),
    )
    expected_lifecycle = (
        LifecycleState.ARCHIVED if delete_mode is DeleteMode.ARCHIVE else LifecycleState.FORGOTTEN
    )

    with pytest.raises(ConflictError):
        kernel.api.update(
            unit.id,
            scope,
            MemoryPatch(content="should not resurrect", mode=update_mode),
            security=security_for(kernel.api, actor),
        )

    all_units = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    assert [item.id for item in all_units] == [unit.id], "拒绝更新时不得残留孤儿新版本"
    assert all_units[0].lifecycle == expected_lifecycle
    assert all_units[0].content == "retired fact"


def test_concurrent_update_on_same_unit_serializes_without_fork() -> None:
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    unit = kernel.api.add("initial fact", scope, security=security_for(kernel.api, actor))[0]
    barrier = threading.Barrier(2)

    def _run(content: str) -> str:
        barrier.wait(timeout=5)
        try:
            updated = kernel.api.update(
                unit.id,
                scope,
                MemoryPatch(content=content),
                security=security_for(kernel.api, actor),
            )
            return f"ok:{updated.id}"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_run, ["branch-1", "branch-2"]))

    assert sorted(outcome.split(":", 1)[0] for outcome in outcomes) == ["conflict", "ok"], outcomes
    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    active = [item for item in listed if item.lifecycle == LifecycleState.ACTIVE]
    assert len(active) == 1, f"并发 update 不应产生双 active: {[item.id for item in active]}"
    assert active[0].supersedes == unit.id


def test_two_runtimes_sharing_kv_serialize_concurrent_update_without_fork() -> None:
    """共享同一 KV 的双运行时基于 KV CAS + 回滚补偿，并发 update 严格 [ok, conflict] 无孤儿。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    shared_kv = InMemoryKVStore()
    kernel_a = build_kernel(kv=shared_kv)
    kernel_b = build_kernel(kv=shared_kv)
    unit = kernel_a.api.add(
        "shared kv fact", scope, security=security_for(kernel_a.api, actor)
    )[0]
    barrier = threading.Barrier(2)

    def _run(pair: tuple[object, str]) -> str:
        kernel, content = pair
        barrier.wait(timeout=5)
        try:
            updated = kernel.api.update(
                unit.id,
                scope,
                MemoryPatch(content=content),
                security=security_for(kernel.api, actor),
            )
            return f"ok:{updated.id}"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(_run, [(kernel_a, "from-runtime-a"), (kernel_b, "from-runtime-b")])
        )

    assert sorted(outcome.split(":", 1)[0] for outcome in outcomes) == ["conflict", "ok"], outcomes
    listed = kernel_a.api.list(scope, security=security_for(kernel_a.api, actor)).items
    active = [item for item in listed if item.lifecycle == LifecycleState.ACTIVE]
    assert len(active) == 1, (
        f"共享 KV 双运行时并发不应产生双 active: {[item.id for item in active]}"
    )
    assert active[0].supersedes == unit.id


def test_supersede_failure_rolls_back_newly_built_unit_and_raises_conflict() -> None:
    """即便废旧阶段抛状态冲突（如跨进程竞态），也必须回滚已写新版并报 ConflictError。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kv = InMemoryKVStore()
    kernel = build_kernel(kv=kv)
    unit = kernel.api.add("origin fact", scope, security=security_for(kernel.api, actor))[0]

    class _ConflictLifecycle(LifecycleManager):
        def operator_type(self) -> ControlOperatorType:
            return ControlOperatorType.LIFECYCLE

        def health(self) -> None:
            return None

        def transition(self, scope: Scope, unit_ids: list[str], target: LifecycleState) -> None:
            return None

        def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
            raise ValidationError("concurrently changed to superseded")

        def sweep(self) -> list[SweepTransition]:
            return []

    setattr(getattr(kernel.api, "_engine"), "_lifecycle", _ConflictLifecycle())

    with pytest.raises(ConflictError):
        kernel.api.update(
            unit.id,
            scope,
            MemoryPatch(content="should rollback"),
            security=security_for(kernel.api, actor),
        )

    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    assert [item.id for item in listed] == [unit.id], "废旧失败后新版必须被回滚，不能残留孤儿"


def _run_update_forget_race_round(
    kernel: object, scope: Scope, actor: Scope, round_idx: int
) -> None:
    unit = kernel.api.add(
        f"race fact {round_idx}", scope, security=security_for(kernel.api, actor)
    )[0]
    barrier = threading.Barrier(2)

    def _do_update() -> str:
        barrier.wait(timeout=5)
        try:
            new_unit = kernel.api.update(
                unit.id,
                scope,
                MemoryPatch(content=f"updated {round_idx}"),
                security=security_for(kernel.api, actor),
            )
            return f"updated:{new_unit.id}"
        except ConflictError:
            return "conflict"

    def _do_forget() -> list[str]:
        barrier.wait(timeout=5)
        return kernel.api.delete(
            DeleteSelector(unit_ids=[unit.id], scope=scope, mode=DeleteMode.FORGET),
            security=security_for(kernel.api, actor),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_update = pool.submit(_do_update)
        f_forget = pool.submit(_do_forget)
        update_result = f_update.result(timeout=5)
        forget_result = f_forget.result(timeout=5)

    stored_origin = kernel.api.get(unit.id, scope, security=security_for(kernel.api, actor))
    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    successors = [item for item in listed if item.supersedes == unit.id]
    if update_result == "conflict":
        assert forget_result == [unit.id]
        assert stored_origin.lifecycle == LifecycleState.FORGOTTEN
        assert successors == [], "forget 获胜且 update 报错时绝不能留下可见后继"
    else:
        assert stored_origin.lifecycle in (
            LifecycleState.SUPERSEDED,
            LifecycleState.FORGOTTEN,
        )
        assert len(successors) == 1 and successors[0].lifecycle == LifecycleState.ACTIVE


def test_concurrent_update_and_forget_never_leave_orphan_active() -> None:
    """同一引擎内 update(A) 与 forget(A) 并发交错，失败的纠正绝不能留下可见 active 后继。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()

    for round_idx in range(10):
        _run_update_forget_race_round(kernel, scope, actor, round_idx)


def test_supersede_build_partial_failure_rolls_back_new_keeping_only_old_active() -> None:
    """新版已写本体但建索引中途失败时，必须回滚新版，确保只剩旧版 A 为唯一 ACTIVE。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    old = kernel.api.add("origin fact", scope, security=security_for(kernel.api, actor))[0]
    engine = getattr(kernel.api, "_engine")
    real_index = getattr(engine, "_index")

    class _FailAfterForwardBuild:
        def __init__(self, delegate) -> None:
            self._delegate = delegate

        def build(self, units, **kwargs) -> None:
            self._delegate.build(units, **kwargs)
            raise BackendError("fulltext index failed after forward truth write")

        def update(self, units, **kwargs) -> None:
            self._delegate.update(units, **kwargs)

        def remove(self, units, **kwargs) -> None:
            self._delegate.remove(units, **kwargs)

    setattr(engine, "_index", _FailAfterForwardBuild(real_index))

    with pytest.raises(BackendError):
        kernel.api.update(
            old.id,
            scope,
            MemoryPatch(content="new fact"),
            security=security_for(kernel.api, actor),
        )

    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    active = [item for item in listed if item.lifecycle == LifecycleState.ACTIVE]
    assert [item.id for item in active] == [old.id], "build 中途失败后只能保留原 ACTIVE 版本 A"


def test_supersede_secondary_index_refresh_failure_preserves_sole_new_active() -> None:
    """旧版已在真源提交为 SUPERSEDED 后，二级索引刷新失败绝不能误删唯一现行新版 B。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    actor = scope
    kernel = build_kernel()
    old = kernel.api.add("origin fact", scope, security=security_for(kernel.api, actor))[0]
    engine = getattr(kernel.api, "_engine")
    real_index = getattr(engine, "_index")

    class _FailOldIndexUpdate:
        def __init__(self, delegate) -> None:
            self._delegate = delegate

        def build(self, units, **kwargs) -> None:
            self._delegate.build(units, **kwargs)

        @staticmethod
        def update(units, **kwargs) -> None:
            raise BackendError("secondary index refresh failed after supersede commit")

        def remove(self, units, **kwargs) -> None:
            self._delegate.remove(units, **kwargs)

    setattr(engine, "_index", _FailOldIndexUpdate(real_index))

    with pytest.raises(BackendError):
        kernel.api.update(
            old.id,
            scope,
            MemoryPatch(content="new fact"),
            security=security_for(kernel.api, actor),
        )

    listed = kernel.api.list(scope, security=security_for(kernel.api, actor)).items
    active = [item for item in listed if item.lifecycle == LifecycleState.ACTIVE]
    assert len(active) == 1 and active[0].supersedes == old.id, (
        "旧版已 SUPERSEDED 后二级索引刷新失败不得删掉唯一现行新版 B"
    )
    stored_old = kernel.api.get(old.id, scope, security=security_for(kernel.api, actor))
    assert stored_old.lifecycle == LifecycleState.SUPERSEDED


@pytest.mark.parametrize(
    ("stored_invalid_at", "expected_invalid_at", "expected_rollback"),
    [
        (
            datetime(2026, 6, 17, 11, 0, tzinfo=UTC),
            datetime(2026, 6, 17, 11, 0),
            False,
        ),
        (
            datetime(2026, 6, 17, 11, 0),
            datetime(2026, 6, 17, 19, 0, tzinfo=timezone(timedelta(hours=8))),
            False,
        ),
        (
            datetime(2026, 6, 17, 11, 0, 1, tzinfo=UTC),
            datetime(2026, 6, 17, 11, 0),
            True,
        ),
        (
            None,
            datetime(2026, 6, 17, 11, 0, tzinfo=UTC),
            True,
        ),
    ],
)
def test_should_rollback_after_supersede_error_normalizes_t_invalid_to_utc(
    stored_invalid_at: datetime | None,
    expected_invalid_at: datetime,
    expected_rollback: bool,
) -> None:
    """t_invalid 比较双侧过 as_utc：aware/naive 混合同 UTC 时刻不误判回滚，缺值仍回滚。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    stored = MemoryUnit(
        id="u-old",
        scope=scope,
        lifecycle=LifecycleState.SUPERSEDED,
        temporal=Temporal(t_invalid=stored_invalid_at),
    )

    result = should_rollback_after_supersede_error(
        lambda _scope, _unit_id: stored,
        scope,
        stored.id,
        expected_invalid_at,
    )
    assert result is expected_rollback, (
        f"stored={stored_invalid_at!r}, expected={expected_invalid_at!r} -> {result}"
    )


def test_finalize_supersede_unconditionally_rolls_back_on_conflict_error() -> None:
    """废旧阶段抛 ConflictError 时必须无条件回滚新版，不依赖 should_rollback 重读结果。"""
    scope = Scope(org="acme", user="u1", agent="a1", session="s1")
    new_unit = MemoryUnit(id="u-new", scope=scope, supersedes="u-old")
    rolled_back: list[str] = []
    should_rollback_called = False

    def _fail_conflict() -> None:
        raise ConflictError("memory_unit", "u-old", message="already superseded")

    def _should_rollback() -> bool:
        nonlocal should_rollback_called
        should_rollback_called = True
        return False

    with pytest.raises(ConflictError):
        finalize_supersede_or_rollback(
            new_unit=new_unit,
            finalize_old=_fail_conflict,
            rollback_new=lambda: rolled_back.append(new_unit.id),
            map_validation_error=False,
            should_rollback=_should_rollback,
        )

    assert rolled_back == ["u-new"], "ConflictError 必须无条件回滚新版"
    assert not should_rollback_called, "ConflictError 不应再调用 should_rollback"
