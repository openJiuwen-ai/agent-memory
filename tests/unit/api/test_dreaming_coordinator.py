"""API 层 DreamingCoordinator —— 注册 / 注销 / 恢复 / 持续授权 / fan-out 逐桶裁决。

PEP 边界（S03）改造后的 dreaming 编排测试（F03）：

- 注册（dreaming=True）：调度器周期能力校验 → validate 先于落盘（无残留）→
  注册表写入 dict DSL + created_by（持续授权锚点）；
- 注销（dreaming=False）：幂等——命中则 cancel + 删注册项，未命中静默 None，
  **永不退化立即跑**；
- 驱动 tick（DreamingDriverJob.run）：复验授权（拒绝 → 注销 + 抛错停父定时器）
  → fan-out 逐桶裁决（获准桶 / 拒绝桶标签）→ 经 commands.evolve 提交一次性
  EvolveJob（唯一执行链条）；
- 恢复（restore）：单实例锁 → 逐条以 created_by 复验 → 换绑新 job_id；
  旧版无主 entry / 损坏值 fail-closed 跳过；拒绝即注销；幂等缓存。

控制层纯执行链的测试在 ``tests/unit/control/test_dreaming_dispatch.py``。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.api.memory_api_impl.dreaming import (
    DreamingCoordinator,
    DreamingDriverJob,
    actor_to_dict,
)
from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import (
    FanOutCandidate,
    MemoryUnit,
    PredicateCandidate,
    Scope,
    Segment,
    Temporal,
    memory_key,
)
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.config import Config
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.engine_impl.dreaming_registry import (
    DreamingEntry,
    DreamingRegistry,
)
from jiuwen_memory.control.scheduler import RecurringJobStoppedError
from jiuwen_memory.control.scheduler_impl.async_timer_scheduler import AsyncTimerScheduler
from jiuwen_memory.control.scheduler_impl.in_process_scheduler import InProcessScheduler
from jiuwen_memory.control.types import Channel, JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="coding", user="alice")
_ACTOR = Scope(org="acme", space="coding", user="alice", agent="cron")


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _FakeScheduler:
    """支持周期任务的调度器替身：记录 submit / cancel，不真触发定时。"""

    supports_recurring = True

    def __init__(self) -> None:
        self.submitted: list[tuple[str, object, Channel]] = []
        self.cancelled: list[str] = []

    def validate(self, job) -> None:
        if job.interval <= 0:
            raise ValueError("interval must be > 0 for recurring job")

    async def submit(self, job, channel) -> str:
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        self.submitted.append((job_id, job, channel))
        return job_id

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    def status(self, job_id: str):  # pragma: no cover - 协调器不查询状态
        raise NotImplementedError


class _FakeCommands:
    """commands.evolve 替身：记录调用（tick 提交的一次性演进）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def evolve(
        self,
        scope,
        mode,
        channel=Channel.BACKGROUND,
        *,
        candidate=None,
        buckets=None,
        denied_scopes=None,
    ) -> str:
        self.calls.append(
            {
                "scope": scope,
                "mode": mode,
                "channel": channel,
                "candidate": candidate,
                "buckets": buckets,
                "denied_scopes": denied_scopes,
            }
        )
        return f"spawn-{len(self.calls)}"


class _FakeApi:
    """LocalMemoryAPI 最小替身：只实现协调器反向引用的鉴权与组件属性。"""

    def __init__(self, scheduler, kv) -> None:
        self._scheduler = scheduler
        self._engine = SimpleNamespace(kv=kv)
        self._commands = _FakeCommands()
        self.denied: list[Scope] = []
        self.authorized: list[tuple[Scope, Scope]] = []

    def _authorize(self, identity, target, action, entry, *, space_action=None, **_kw):
        self.authorized.append((identity, target))
        if target in self.denied:
            raise PermissionDeniedError(action.value)

    def _ensure_space_writable(self, scope) -> None:
        return None


def _coordinator(api: _FakeApi) -> DreamingCoordinator:
    return DreamingCoordinator(api, registry=DreamingRegistry(api._engine.kv))


def test_registration_key_is_unambiguous_for_separator_values() -> None:
    left = Scope(org="a/b", space="c")
    right = Scope(org="a", space="b/c")

    assert DreamingRegistry.key_of(left, "extract") != DreamingRegistry.key_of(
        right, "extract"
    )


def test_registry_uses_configured_lock_provider_lease() -> None:
    lock = InMemoryLockProvider(lease_ms=1_234, wait_timeout_ms=0)
    registry = DreamingRegistry(InMemoryKVStore(), lock=lock)

    assert registry.acquire_instance_lock()
    assert registry._lock_handle is not None
    assert registry._lock_handle.lease_ms == 1_234
    registry.release_instance_lock()


def _make_unit(uid: str, scope: Scope) -> MemoryUnit:
    """占位 unit——只为让 ``kv.scopes()`` 枚举出该桶，内容不参与断言。"""
    return MemoryUnit(
        id=uid,
        scope=scope,
        segments=[Segment(content=f"content of {uid}")],
        temporal=Temporal(),
    )


def _insert_bucket(kv: InMemoryKVStore, scope: Scope) -> None:
    uid = f"u-{scope.user or scope.org}"
    kv.insert(scope, memory_key(uid), dumps(_make_unit(uid, scope)))


def _register(
    coordinator: DreamingCoordinator,
    scheduler: _FakeScheduler,
    *,
    scope: Scope = _SCOPE,
    mode: EvolveMode = EvolveMode.FORGET,
    candidate=None,
    interval: int = 60,
    actor: Scope = _ACTOR,
) -> str:
    job_id = coordinator.register(
        scope=scope,
        mode=mode,
        channel=Channel.BACKGROUND,
        candidate=candidate,
        interval=interval,
        actor=actor,
    )
    job = scheduler.submitted[0][1]
    assert isinstance(job, DreamingDriverJob), "注册提交的是 API 层驱动任务"
    return job_id


def test_non_leader_unregister_does_not_delete_shared_registration() -> None:
    kv = InMemoryKVStore()
    leader_api = _FakeApi(_FakeScheduler(), kv)
    follower_api = _FakeApi(_FakeScheduler(), kv)
    leader = _coordinator(leader_api)
    follower = _coordinator(follower_api)
    _register(leader, leader_api._scheduler)

    with pytest.raises(RuntimeError, match="不是 leader"):
        follower.unregister(_SCOPE, EvolveMode.FORGET)

    assert DreamingRegistry(kv).find(_SCOPE, "forget") is not None
    leader.unregister(_SCOPE, EvolveMode.FORGET)
    leader.release_lock()


def test_corrupt_created_by_isolated_from_healthy_registry_entries() -> None:
    kv = InMemoryKVStore()
    registry = DreamingRegistry(kv)
    registry.save(
        DreamingEntry(
            scope=_SCOPE,
            mode="forget",
            interval=60,
            candidate=None,
            job_id="bad",
            channel="background",
            created_by="broken",  # type: ignore[arg-type]
        )
    )
    healthy_scope = Scope(org="acme", space="coding", user="bob")
    registry.save(
        DreamingEntry(
            scope=healthy_scope,
            mode="extract",
            interval=60,
            candidate=None,
            job_id="healthy",
            channel="background",
            created_by=actor_to_dict(_ACTOR),
        )
    )

    entries = registry.load_all()

    assert [(entry.scope, entry.job_id) for entry in entries] == [
        (healthy_scope, "healthy")
    ]


def test_space_validation_stops_recurring_driver_and_removes_registration() -> None:
    class _ReadOnlyApi(_FakeApi):
        def _ensure_space_writable(self, scope) -> None:
            raise ValidationError("space is archived")

    kv = InMemoryKVStore()
    scheduler = _FakeScheduler()
    api = _ReadOnlyApi(scheduler, kv)
    coordinator = _coordinator(api)
    _register(coordinator, scheduler)
    job = scheduler.submitted[0][1]

    with pytest.raises(RecurringJobStoppedError) as caught:
        asyncio.run(job.run())

    assert caught.value.reason == "space_not_writable"
    assert DreamingRegistry(kv).find(_SCOPE, "forget") is None
    coordinator.release_lock()


def test_concurrent_registration_keeps_one_live_timer_and_one_entry() -> None:
    kv = InMemoryKVStore()
    scheduler = AsyncTimerScheduler(tick_interval=1)
    api = _FakeApi(scheduler, kv)
    coordinator = _coordinator(api)

    def register_once(_: int) -> str:
        return coordinator.register(
            scope=_SCOPE,
            mode=EvolveMode.FORGET,
            channel=Channel.BACKGROUND,
            candidate=None,
            interval=60,
            actor=_ACTOR,
        )

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            job_ids = list(pool.map(register_once, range(8)))
        assert len(set(job_ids)) == 1
        assert len(DreamingRegistry(kv).load_all()) == 1
        assert scheduler.status(job_ids[0]).status == JobStatus.RUNNING
        coordinator.unregister(_SCOPE, EvolveMode.FORGET)
    finally:
        coordinator.release_lock()
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# 注册（dreaming=True）
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_persists_entry_with_created_by(self) -> None:
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)

        job_id = _register(
            coordinator,
            scheduler,
            candidate=PredicateCandidate(window=3600),
            interval=86400,
        )

        assert job_id == scheduler.submitted[0][0]
        entries = DreamingRegistry(kv).load_all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.scope == _SCOPE
        assert entry.mode == "forget"
        assert entry.interval == 86400
        assert entry.job_id == job_id
        assert entry.channel == "background"
        assert entry.candidate == {
            "type": "predicate",
            "filters": None,
            "window": 3600,
        }, "候选源以 dict DSL 持久化（数据不是代码）"
        assert entry.created_by == {
            "org": "acme", "space": "coding", "user": "alice",
            "agent": "cron", "session": "",
        }, "#2：注册表记录创建者身份（created_by 持续授权锚点）"

    def test_register_requires_recurring_scheduler(self) -> None:
        """#5：非周期调度器上注册 = 静默跑一次的定时器——注册处 fail fast。"""
        kv = InMemoryKVStore()
        api = _FakeApi(InProcessScheduler(), kv)
        coordinator = _coordinator(api)

        with pytest.raises(ValidationError, match="调度器"):
            _register(coordinator, api._scheduler)

        assert DreamingRegistry(kv).load_all() == [], "拒绝的注册不落盘"

    def test_register_rejected_before_any_side_effect(self) -> None:
        """validate 抛错（interval 低于精度）→ 注册表不产生残留。"""

        class _Invalidating(_FakeScheduler):
            def validate(self, job) -> None:
                raise ValueError("interval below tick precision")

        kv = InMemoryKVStore()
        scheduler = _Invalidating()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)

        with pytest.raises(ValueError, match="tick"):
            coordinator.register(
                scope=_SCOPE,
                mode=EvolveMode.FORGET,
                channel=Channel.BACKGROUND,
                candidate=None,
                interval=1,
                actor=_ACTOR,
            )

        assert DreamingRegistry(kv).load_all() == [], "校验全部先于落盘"

    def test_re_register_same_scope_mode_overrides(self) -> None:
        """同 (scope, mode) 重复注册 = 换绑——注册表只留一条，interval 覆写。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)

        _register(coordinator, scheduler, interval=60)
        second = _register_via_fresh(coordinator, scheduler, interval=3600)

        entries = DreamingRegistry(kv).load_all()
        assert len(entries) == 1, "同 (scope, mode) 一条注册"
        assert entries[0].interval == 3600
        assert entries[0].job_id == second

    def test_different_modes_coexist(self) -> None:
        """同 scope 不同 mode 的注册互不覆盖（schedule_key 含 mode）。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)

        _register(coordinator, scheduler, interval=60)
        _register_via_fresh(
            coordinator, scheduler, mode=EvolveMode.EXTRACT, interval=120
        )

        assert {e.mode for e in DreamingRegistry(kv).load_all()} == {
            "forget", "extract"
        }

    def test_register_save_failure_rolls_back_submitted_job(self) -> None:
        """D6 写序契约：落盘失败回滚 cancel——不留注销不掉的幽灵定时器。"""

        class _SaveFailing(DreamingRegistry):
            def save(self, entry: DreamingEntry) -> None:
                raise RuntimeError("kv write failed")

        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = DreamingCoordinator(api, registry=_SaveFailing(kv))

        with pytest.raises(RuntimeError, match="kv write failed"):
            coordinator.register(
                scope=_SCOPE,
                mode=EvolveMode.FORGET,
                channel=Channel.BACKGROUND,
                candidate=None,
                interval=60,
                actor=_ACTOR,
            )

        assert scheduler.cancelled == [scheduler.submitted[0][0]], (
            "已提交的定时器被回滚取消"
        )
        assert DreamingRegistry(kv).load_all() == [], "落盘失败的注册不留记录"

    def test_update_save_failure_restores_previous_timer(self) -> None:
        """更新复用旧 job_id 时，落盘失败不得 cancel 原健康定时器。"""

        class _FailSecondSave(DreamingRegistry):
            def __init__(self, kv) -> None:
                super().__init__(kv)
                self.saves = 0

            def save(self, entry: DreamingEntry) -> None:
                self.saves += 1
                if self.saves == 2:
                    raise RuntimeError("kv write failed")
                super().save(entry)

        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        registry = _FailSecondSave(kv)
        coordinator = DreamingCoordinator(api, registry=registry)
        coordinator.register(
            scope=_SCOPE,
            mode=EvolveMode.FORGET,
            channel=Channel.BACKGROUND,
            candidate=None,
            interval=60,
            actor=_ACTOR,
        )

        with pytest.raises(RuntimeError, match="kv write failed"):
            coordinator.register(
                scope=_SCOPE,
                mode=EvolveMode.FORGET,
                channel=Channel.BACKGROUND,
                candidate=None,
                interval=120,
                actor=_ACTOR,
            )

        assert scheduler.cancelled == [], "已有任务应恢复旧声明而不是被取消"
        assert scheduler.submitted[-1][1].interval == 60
        assert DreamingRegistry(kv).find(_SCOPE, "forget").interval == 60
        coordinator.release_lock()

    def test_lock_loss_is_serialized_with_inflight_registration(self) -> None:
        """续租回调与注册共用变更锁；save 期间失锁不得成功留下 timer。"""

        class _BlockingSaveRegistry(DreamingRegistry):
            def __init__(self, kv) -> None:
                super().__init__(kv)
                self.leader = True
                self.save_entered = threading.Event()
                self.finish_save = threading.Event()

            @property
            def owns_instance_lock(self) -> bool:
                return self.leader

            def acquire_instance_lock(self, ttl_seconds=None) -> bool:
                del ttl_seconds
                self.leader = True
                return True

            def save(self, entry: DreamingEntry) -> None:
                self.save_entered.set()
                assert self.finish_save.wait(timeout=5), "test did not release blocked save"
                super().save(entry)

        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        registry = _BlockingSaveRegistry(kv)
        coordinator = DreamingCoordinator(_FakeApi(scheduler, kv), registry=registry)
        errors: list[Exception] = []

        def _register_in_thread() -> None:
            try:
                coordinator.register(
                    scope=_SCOPE,
                    mode=EvolveMode.FORGET,
                    channel=Channel.BACKGROUND,
                    candidate=None,
                    interval=60,
                    actor=_ACTOR,
                )
            except Exception as exc:
                errors.append(exc)

        register_thread = threading.Thread(target=_register_in_thread)
        register_thread.start()
        assert registry.save_entered.wait(timeout=5), "register did not enter save"

        registry.leader = False
        callback_thread = threading.Thread(target=registry._notify_lock_lost)
        callback_thread.start()
        registry.finish_save.set()
        register_thread.join(timeout=5)
        callback_thread.join(timeout=5)

        assert not register_thread.is_alive()
        assert not callback_thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RecurringJobStoppedError)
        assert errors[0].reason == "leadership_lost"
        assert scheduler.cancelled == [scheduler.submitted[0][0]]
        assert coordinator._active_jobs == {}
        assert coordinator._restored is None


def _register_via_fresh(
    coordinator: DreamingCoordinator,
    scheduler: _FakeScheduler,
    *,
    mode: EvolveMode = EvolveMode.FORGET,
    interval: int = 60,
) -> str:
    """第二条注册（复用同一 coordinator / scheduler，取最新 submit）。"""
    job_id = coordinator.register(
        scope=_SCOPE,
        mode=mode,
        channel=Channel.BACKGROUND,
        candidate=None,
        interval=interval,
        actor=_ACTOR,
    )
    assert scheduler.submitted[-1][0] == job_id
    return job_id


# ---------------------------------------------------------------------------
# 注销（dreaming=False）
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_unregister_cancels_job_and_deletes_entry(self) -> None:
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        job_id = _register(coordinator, scheduler)

        entry = coordinator.unregister(_SCOPE, EvolveMode.FORGET)

        assert entry is not None
        assert entry.job_id == job_id
        assert scheduler.cancelled == [job_id], "定时任务被 cancel"
        assert DreamingRegistry(kv).load_all() == [], "注册项已删"

    def test_unregister_missing_is_silent_noop(self) -> None:
        """#1：未注册时 dreaming=False = 幂等注销 no-op，永不退化立即跑。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)

        entry = coordinator.unregister(_SCOPE, EvolveMode.FORGET)

        assert entry is None, "未命中注册 → 静默 None"
        assert api._commands.calls == [], "误调注销绝不触发一次演进"
        assert scheduler.cancelled == []
        assert DreamingRegistry(kv).load_all() == []


# ---------------------------------------------------------------------------
# 驱动 tick（持续授权 + fan-out 逐桶裁决 + 唯一执行链）
# ---------------------------------------------------------------------------


class TestDriverTick:
    def test_tick_submits_one_shot_evolve(self) -> None:
        """tick → 复验通过 → 提交一次性 EvolveJob（经 commands，唯一执行链）。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        _register(coordinator, scheduler)
        job = scheduler.submitted[0][1]

        info = asyncio.run(job.run())

        assert len(api._commands.calls) == 1
        call = api._commands.calls[0]
        assert call["scope"] == _SCOPE
        assert call["mode"] == EvolveMode.FORGET
        assert call["candidate"] is None
        assert call["buckets"] is None, "非 fan-out 候选源无逐桶语义"
        assert call["denied_scopes"] is None
        assert api.authorized == [(_ACTOR, _SCOPE)], "每 tick 以注册者身份复验"
        assert info.status == JobStatus.SUCCEEDED
        assert info.detail["spawned_job_id"] == "spawn-1"

    def test_tick_permission_denied_unregisters_and_raises(self) -> None:
        """#2：注册者授权已撤 → 注销注册项 + 抛错（调度器停父定时器）。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        _register(coordinator, scheduler)
        job = scheduler.submitted[0][1]
        api.denied = [_SCOPE]

        with pytest.raises(PermissionDeniedError):
            asyncio.run(job.run())

        assert DreamingRegistry(kv).load_all() == [], "拒绝即注销（防重启复活）"
        assert api._commands.calls == [], "拒绝后不提交演进"

    def test_tick_fan_out_only_authorized_buckets(self) -> None:
        """#3：fan-out 逐桶裁决——获准桶下发给 Engine，拒绝桶标签回显。"""
        kv = InMemoryKVStore()
        parent = Scope(org="acme")
        alice = Scope(org="acme", user="alice")
        bob = Scope(org="acme", user="bob")
        carol = Scope(org="other", user="carol")

        for scope in (alice, bob, carol):
            _insert_bucket(kv, scope)
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        api.denied = [bob]

        _register(
            coordinator,
            scheduler,
            scope=parent,
            candidate=FanOutCandidate(child=PredicateCandidate()),
        )
        job = scheduler.submitted[0][1]
        asyncio.run(job.run())

        call = api._commands.calls[0]
        assert call["buckets"] == [alice], "只有获准桶下发；carol 不在父覆盖内"
        assert call["denied_scopes"] == ["count:1"], "拒绝资源名脱敏，只回显计数"

    def test_tick_fan_out_all_denied_still_submits_with_labels(self) -> None:
        """全拒：空桶集 + 完整拒绝标签仍走单链提交（不因空集分叉）。"""
        kv = InMemoryKVStore()
        parent = Scope(org="acme")
        alice = Scope(org="acme", user="alice")
        bob = Scope(org="acme", user="bob")

        for scope in (alice, bob):
            _insert_bucket(kv, scope)
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        api.denied = [alice, bob]

        _register(
            coordinator,
            scheduler,
            scope=parent,
            candidate=FanOutCandidate(child=PredicateCandidate()),
        )
        job = scheduler.submitted[0][1]
        info = asyncio.run(job.run())

        call = api._commands.calls[0]
        assert call["buckets"] == []
        assert call["denied_scopes"] == ["count:2"]
        assert info.detail["denied_scopes"] == "count:2"

    def test_driver_schedule_key_isolated_from_one_shot(self) -> None:
        """驱动键 ``dreaming:`` 前缀与一次性 EvolveJob 的 ``evolve:`` 键空间隔离。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        api = _FakeApi(scheduler, kv)
        coordinator = _coordinator(api)
        _register(coordinator, scheduler)

        job = scheduler.submitted[0][1]
        assert job.schedule_key == DreamingRegistry.key_of(_SCOPE, "forget")
        assert job.mode == "forget"


# ---------------------------------------------------------------------------
# fan-out 逐桶裁决（authorize_fan_out 直测）
# ---------------------------------------------------------------------------


class TestAuthorizeFanOut:
    def test_non_fanout_candidate_returns_none_none(self) -> None:
        kv = InMemoryKVStore()
        api = _FakeApi(_FakeScheduler(), kv)
        coordinator = _coordinator(api)

        buckets, denied = coordinator.authorize_fan_out(
            _ACTOR, _SCOPE, PredicateCandidate(window=60), EvolveMode.FORGET
        )

        assert buckets is None
        assert denied is None

    def test_shape_constraints_exclude_agent_buckets(self) -> None:
        """形状约束只收窄：agent 桶既不入获准也不入拒绝（覆盖集外）。"""
        kv = InMemoryKVStore()
        parent = Scope(org="acme")
        user_bucket = Scope(org="acme", user="alice")
        agent_bucket = Scope(org="acme", user="alice", agent="a1")

        _insert_bucket(kv, user_bucket)
        _insert_bucket(kv, agent_bucket)
        api = _FakeApi(_FakeScheduler(), kv)
        coordinator = _coordinator(api)

        buckets, denied = coordinator.authorize_fan_out(
            _ACTOR,
            parent,
            FanOutCandidate(
                child=PredicateCandidate(),
                require_empty=frozenset({"agent", "session"}),
            ),
            EvolveMode.FORGET,
        )

        assert buckets == [user_bucket]
        assert denied is None

    def test_dict_dsl_fan_out_parsed(self) -> None:
        """注册表持久化的 dict DSL（恢复 / tick 路径形态）可解析裁决。"""
        kv = InMemoryKVStore()
        parent = Scope(org="acme")
        alice = Scope(org="acme", user="alice")

        _insert_bucket(kv, alice)
        api = _FakeApi(_FakeScheduler(), kv)
        coordinator = _coordinator(api)

        buckets, denied = coordinator.authorize_fan_out(
            _ACTOR,
            parent,
            {"type": "fan_out", "child": {"filters": None, "window": 60}},
            EvolveMode.FORGET,
        )

        assert buckets == [alice]
        assert denied is None


# ---------------------------------------------------------------------------
# 重启恢复（restore）
# ---------------------------------------------------------------------------


class TestRestore:
    def test_empty_recurring_instance_still_holds_leader_lock(self) -> None:
        """空表启动也要选主，避免随后注册后被第二实例重复恢复。"""
        kv = InMemoryKVStore()
        first = _coordinator(_FakeApi(_FakeScheduler(), kv))

        assert first.restore() == []
        with pytest.raises(RuntimeError, match="实例锁"):
            _coordinator(_FakeApi(_FakeScheduler(), kv)).restore()
        first.release_lock()

    def test_restore_resubmits_and_rewrites_job_id(self) -> None:
        kv = InMemoryKVStore()
        scheduler1 = _FakeScheduler()
        api1 = _FakeApi(scheduler1, kv)
        coordinator1 = _coordinator(api1)
        old_id = _register(coordinator1, scheduler1)
        coordinator1.release_lock()  # 模拟旧进程退出

        # 模拟重启：新调度器（job_id 空间重置），注册表持久在 KV。
        scheduler2 = _FakeScheduler()
        api2 = _FakeApi(scheduler2, kv)
        coordinator2 = _coordinator(api2)
        restored = coordinator2.restore()

        assert len(restored) == 1
        assert restored[0] != old_id, "job_id 重生成"
        assert restored[0] == scheduler2.submitted[0][0]
        entries = DreamingRegistry(kv).load_all()
        assert entries[0].job_id == restored[0], "注册表换绑为新鲜 id"
        assert api2.authorized == [(_ACTOR, _SCOPE)], "恢复期以 created_by 复验"

    def test_restore_save_failure_cancels_new_job_and_keeps_entry(self) -> None:
        """D6 写序在恢复路径：换绑落盘失败 → 回滚新 job，entry 保留旧值。

        不回滚则注册表留着旧 job_id——注销 cancel 旧 id 是 no-op，新定时器
        成注销不掉的幽灵任务。
        """

        class _SaveFailing(DreamingRegistry):
            def save(self, entry: DreamingEntry) -> None:
                raise RuntimeError("kv write failed")

        kv = InMemoryKVStore()
        scheduler1 = _FakeScheduler()
        coordinator1 = _coordinator(_FakeApi(scheduler1, kv))
        _register(coordinator1, scheduler1)
        coordinator1.release_lock()  # 模拟旧进程退出
        old_entries = DreamingRegistry(kv).load_all()

        scheduler2 = _FakeScheduler()
        coordinator2 = DreamingCoordinator(
            _FakeApi(scheduler2, kv), registry=_SaveFailing(kv)
        )

        restored = coordinator2.restore()

        assert restored == []
        assert scheduler2.cancelled == [scheduler2.submitted[0][0]], (
            "换绑落盘失败的新定时器被回滚取消"
        )
        assert DreamingRegistry(kv).load_all() == old_entries, (
            "entry 保留旧值（下次重启可恢复）"
        )

    def test_restore_lock_conflict_raises_runtime_error(self) -> None:
        """#6：另一实例持锁（未过期）→ RuntimeError，不静默双跑。"""
        kv = InMemoryKVStore()
        s1 = _FakeScheduler()
        coordinator1 = _coordinator(_FakeApi(s1, kv))
        _register(coordinator1, s1)

        s3 = _FakeScheduler()
        with pytest.raises(RuntimeError, match="实例锁"):
            _coordinator(_FakeApi(s3, kv)).restore()
        coordinator1.release_lock()

    def test_restore_released_lock_allows_second_instance(self) -> None:
        """优雅关闭释放锁后，第二实例可恢复。"""
        kv = InMemoryKVStore()
        s1 = _FakeScheduler()
        coordinator1 = _coordinator(_FakeApi(s1, kv))
        _register(coordinator1, s1)
        coordinator1.release_lock()

        s2 = _FakeScheduler()
        api2 = _FakeApi(s2, kv)
        coordinator2 = _coordinator(api2)
        assert coordinator2.restore()
        coordinator2.release_lock()

        assert _coordinator(_FakeApi(_FakeScheduler(), kv)).restore()

    def test_restore_empty_registry_on_in_process_scheduler_is_noop(self) -> None:
        """空注册表 + 默认调度器 = no-op——默认装配的 server 启动不被阻断。"""
        kv = InMemoryKVStore()
        api = _FakeApi(InProcessScheduler(), kv)

        assert _coordinator(api).restore() == []

    def test_restore_skips_legacy_entries_without_created_by(self) -> None:
        """#2：旧版本 entry 无 created_by → fail-closed 跳过，记录保留。"""
        kv = InMemoryKVStore()
        DreamingRegistry(kv).save(
            DreamingEntry(
                scope=_SCOPE, mode="forget", interval=60,
                candidate=None, job_id="legacy", channel="background",
                created_by=None,
            )
        )
        api = _FakeApi(_FakeScheduler(), kv)

        restored = _coordinator(api).restore()

        assert restored == []
        assert len(DreamingRegistry(kv).load_all()) == 1, "记录保留（修复后下次可恢复）"

    def test_restore_denied_unregisters(self) -> None:
        """#2：注册者授权已失效 → 不复活，注销该条（拒绝即停同语义）。"""
        kv = InMemoryKVStore()
        scheduler1 = _FakeScheduler()
        api1 = _FakeApi(scheduler1, kv)
        coordinator1 = _coordinator(api1)
        _register(coordinator1, scheduler1)
        coordinator1.release_lock()

        scheduler2 = _FakeScheduler()
        api2 = _FakeApi(scheduler2, kv)
        api2.denied = [_SCOPE]

        restored = _coordinator(api2).restore()

        assert restored == []
        assert DreamingRegistry(kv).load_all() == [], "拒绝的注册被注销"

    def test_restore_skips_entries_with_bad_mode_or_channel(self) -> None:
        """损坏的 mode / channel 取值 → 跳过并保留（装配修复后下次可恢复）。"""
        kv = InMemoryKVStore()
        registry = DreamingRegistry(kv)

        registry.save(
            DreamingEntry(
                scope=_SCOPE, mode="bogus_mode", interval=60,
                candidate=None, job_id="j1", channel="background",
                created_by=actor_to_dict(_ACTOR),
            )
        )
        registry.save(
            DreamingEntry(
                scope=_SCOPE, mode="forget", interval=60,
                candidate=None, job_id="j2", channel="bogus_channel",
                created_by=actor_to_dict(_ACTOR),
            )
        )
        api = _FakeApi(_FakeScheduler(), kv)

        restored = _coordinator(api).restore()

        assert restored == []
        assert len(DreamingRegistry(kv).load_all()) == 2, "损坏记录保留"

    def test_restore_requires_recurring_scheduler(self) -> None:
        """#5：非周期调度器上有待恢复条目 = 退化跑一次——拒绝恢复。"""
        kv = InMemoryKVStore()
        DreamingRegistry(kv).save(
            DreamingEntry(
                scope=_SCOPE, mode="forget", interval=60,
                candidate=None, job_id="j1", channel="background",
                created_by=actor_to_dict(_ACTOR),
            )
        )
        api = _FakeApi(InProcessScheduler(), kv)

        with pytest.raises(ValidationError, match="调度器"):
            _coordinator(api).restore()

    def test_restore_idempotent_returns_cached_result(self) -> None:
        """幂等缓存：二次调用返回首次结果，不重扫注册表。"""
        kv = InMemoryKVStore()
        scheduler1 = _FakeScheduler()
        api1 = _FakeApi(scheduler1, kv)
        coordinator1 = _coordinator(api1)
        _register(coordinator1, scheduler1)
        coordinator1.release_lock()

        scheduler2 = _FakeScheduler()
        api2 = _FakeApi(scheduler2, kv)
        coordinator2 = _coordinator(api2)

        first = coordinator2.restore()
        second = coordinator2.restore()

        assert first == second
        assert len(scheduler2.submitted) == 1, "不重扫重提"

    def test_release_invalidates_restore_cache_for_next_leader_term(self) -> None:
        """恢复幂等性只在一次 leader 任期内有效。"""
        kv = InMemoryKVStore()
        scheduler = _FakeScheduler()
        coordinator = _coordinator(_FakeApi(scheduler, kv))

        assert coordinator.restore() == []
        coordinator.release_lock()
        DreamingRegistry(kv).save(
            DreamingEntry(
                scope=_SCOPE,
                mode="forget",
                interval=60,
                candidate=None,
                job_id="written-by-other-leader",
                channel="background",
                created_by=actor_to_dict(_ACTOR),
            )
        )

        restored = coordinator.restore()

        assert len(restored) == 1
        assert restored == [scheduler.submitted[0][0]]
        assert DreamingRegistry(kv).find(_SCOPE, "forget").job_id == restored[0]
        coordinator.release_lock()

    def test_restore_aborts_and_cancels_when_leadership_is_lost_during_save(self) -> None:
        """单条换绑 save 期间失锁时，恢复不继续后续记录。"""

        class _LoseLeadershipOnSave(DreamingRegistry):
            def __init__(self, kv) -> None:
                super().__init__(kv)
                self.leader = False

            @property
            def owns_instance_lock(self) -> bool:
                return self.leader

            def acquire_instance_lock(self, ttl_seconds=None) -> bool:
                del ttl_seconds
                self.leader = True
                return True

            def save(self, entry: DreamingEntry) -> None:
                super().save(entry)
                self.leader = False

        kv = InMemoryKVStore()
        DreamingRegistry(kv).save(
            DreamingEntry(
                scope=_SCOPE,
                mode="forget",
                interval=60,
                candidate=None,
                job_id="old-job",
                channel="background",
                created_by=actor_to_dict(_ACTOR),
            )
        )
        scheduler = _FakeScheduler()
        coordinator = DreamingCoordinator(
            _FakeApi(scheduler, kv), registry=_LoseLeadershipOnSave(kv)
        )

        with pytest.raises(RecurringJobStoppedError) as caught:
            coordinator.restore()

        assert caught.value.reason == "leadership_lost"
        assert scheduler.cancelled == [scheduler.submitted[0][0]]
        assert coordinator._active_jobs == {}
        assert coordinator._restored is None


# ---------------------------------------------------------------------------
# query_ops 三态分发（真实装配，默认 in_process 调度器）
# ---------------------------------------------------------------------------


class TestQueryOpsThreeStates:
    _KSCOPE = Scope(org="acme", user="owner")

    @staticmethod
    def _api():
        cfg = Config.from_dict({"permission": {"default": "sqlite"}})
        return build_kernel(config=cfg).api

    def test_register_interval_must_be_positive(self) -> None:
        api = self._api()
        with pytest.raises(ValidationError, match="interval"):
            api.evolve(
                self._KSCOPE,
                EvolveMode.EXTRACT,
                dreaming=True,
                interval=0,
                security=legacy_request_context(self._KSCOPE),
            )

    def test_register_rejected_on_in_process_scheduler(self) -> None:
        api = self._api()
        with pytest.raises(ValidationError, match="调度器"):
            api.evolve(
                self._KSCOPE,
                EvolveMode.EXTRACT,
                dreaming=True,
                interval=60,
                security=legacy_request_context(self._KSCOPE),
            )

    def test_unregister_missing_returns_none(self) -> None:
        """#1：未注册时注销 = 幂等 no-op，返回 None 不跑演进。"""
        api = self._api()
        result = api.evolve(
            self._KSCOPE,
            EvolveMode.EXTRACT,
            dreaming=False,
            security=legacy_request_context(self._KSCOPE),
        )
        assert result is None
