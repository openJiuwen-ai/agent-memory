# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""API 层 dreaming 编排（F04 —— 定时演进的注册 / 注销 / 恢复 / 持续授权）。

PEP 边界（S03）下的分工：Control 层（Engine / EvolveJob / resolver）是**纯执行件**
——不认识 actor、不做鉴权；本模块是 API 层的 dreaming 调度服务，承担全部授权语义：

- **入口鉴权**（query_ops.evolve）：WRITE + 演进空间动作 + 空间可写，只在当次有效；
- **持续授权**（每 tick）：:class:`DreamingDriverJob` 以注册者 actor 复验同一套判定
  （:meth:`DreamingCoordinator.check_authorization`）——拒绝即注销注册表并抛
  :class:`~common.errors.PermissionDeniedError`，调度器停父定时器并记
  ``stopped_reason=permission_denied``；
- **fan-out 逐桶裁决**（立即路径与每 tick 共用
  :meth:`DreamingCoordinator.authorize_fan_out`）：枚举命中桶 → 逐桶鉴权 +
  空间可写校验 → 只把获准桶下发给 Engine（``buckets`` / ``denied_scopes``），
  拒绝桶标签回显不静默吞、不中断其余桶。

驱动链：DriverJob（定时）→ 每 tick 复验 + 逐桶裁决 → ``commands.evolve`` →
Engine.evolve → 一次性 EvolveJob（唯一执行链条，F04 红线：不得另写执行链）。
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Callable

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.types import Action
from jiuwen_memory.common.type_def import (
    CandidateSource,
    FanOutCandidate,
    Scope,
    candidate_from_dict,
    candidate_to_dict,
)
from jiuwen_memory.construction import EvolveMode
from jiuwen_memory.control.engine_impl.dreaming_registry import (
    _SCOPE_FIELDS,
    DreamingEntry,
    DreamingRegistry,
    actor_from_dict,
    actor_to_dict,
)
from jiuwen_memory.control.jobs import Job
from jiuwen_memory.control.jobs_impl.candidate_resolver import (
    MAX_FAN_OUT_BUCKETS,
    covered_by,
)
from jiuwen_memory.control.scheduler import RecurringJobStoppedError, Scheduler
from jiuwen_memory.control.types import Channel, JobInfo, JobStatus

from .local_support import _evolve_space_action

logger = get_logger(__name__)


def _scope_label(scope: Scope) -> str:
    """桶标签（denied_scopes 回显形态）——五元组扁平化，与注册键同格式。"""
    return "/".join(getattr(scope, f) or "" for f in _SCOPE_FIELDS)


def _candidate_dsl(candidate: CandidateSource | dict | None) -> dict | None:
    """候选源 → 经完整校验、规范化的可持久化 dict DSL。"""
    if candidate is None:
        return None
    if isinstance(candidate, dict):
        return candidate_to_dict(candidate_from_dict(candidate))
    # dataclass 可能在构造后被调用方改写；round-trip 再校验一次，确保不会把
    # 失效对象写进长命注册表。
    return candidate_to_dict(candidate_from_dict(candidate_to_dict(candidate)))


def _parse_fan_out(candidate: CandidateSource | dict | None) -> FanOutCandidate | None:
    """candidate → FanOutCandidate；非 fan-out（None/谓词/点名/召回）→ None。

    损坏的 dict DSL 会在此抛错——立即路径与 tick 路径同语义失败，不静默降级
    （降级成谓词全量源会把「本该枚举的桶」跑成「scope 直查」，方向错误）。
    """
    if candidate is None:
        return None
    parsed = candidate if not isinstance(candidate, dict) else candidate_from_dict(candidate)
    return parsed if isinstance(parsed, FanOutCandidate) else None


class DreamingDriverJob(Job):
    """dreaming 驱动任务（API 层）——每 tick：复验授权 → fan-out 裁决 → 提交 EvolveJob。

    持续授权的载体（F04 D7）：一次性入口鉴权不覆盖任务存续期——本 Job 持有
    注册者 actor，每 tick 以它重新走 API 层鉴权（与入口同一套组件，判定不漂移）。
    拒绝 → 注销注册表（防重启复活）→ 抛 PermissionDeniedError（调度器停父
    定时器）。任务本体**不执行演进**——每 tick 经 ``commands.evolve`` 提交一次性
    EvolveJob 走唯一执行链，演进发生在 EvolveJob 内。
    """

    def __init__(
        self,
        coordinator: DreamingCoordinator,
        *,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel,
        candidate: CandidateSource | dict | None,
        actor: Scope,
        interval: int,
    ) -> None:
        super().__init__(scope=scope, interval=interval)
        self._coordinator = coordinator
        self._mode = mode
        self._channel = channel
        self._candidate = candidate
        self._actor = actor

    @property
    def mode(self) -> str:
        return self._mode.value

    @property
    def schedule_key(self) -> str:
        """调度去重键：同 scope 下不同 mode 的注册是不同任务（含 mode）。

        前缀 ``dreaming:`` 与一次性 EvolveJob 的 ``evolve:`` 键空间隔离——
        同 scope 的注册态驱动与一次性演进互不干扰。
        """
        return self._coordinator.registration_key(self.scope, self._mode)

    async def run(self) -> JobInfo:
        self.check_cancelled()
        coordinator = self._coordinator
        coordinator.check_leadership()
        # 1) 每 tick 复验（持续授权）——拒绝即注销 + 抛错（调度器停父定时器）。
        try:
            coordinator.check_authorization(self._actor, self.scope, self._mode)
        except PermissionDeniedError:
            coordinator.remove_registration(self.scope, self._mode)
            raise
        except ValidationError as exc:
            coordinator.remove_registration(self.scope, self._mode)
            raise RecurringJobStoppedError(
                "space_not_writable", str(exc)
            ) from exc
        # 2) fan-out 逐桶裁决（API 层）：获准桶下发给 Engine，拒绝桶回显。
        buckets, denied = coordinator.authorize_fan_out(
            self._actor, self.scope, self._candidate, self._mode
        )
        # 3) 提交一次性 EvolveJob（唯一执行链条）——演进在 EvolveJob 内发生。
        self.check_cancelled()
        coordinator.check_leadership()
        spawned = await coordinator.submit_evolve(
            scope=self.scope,
            mode=self._mode,
            channel=self._channel,
            candidate=self._candidate,
            buckets=buckets,
            denied_scopes=denied,
        )
        if spawned and self.parent_job_id:
            coordinator.link_spawned_job(spawned, self.parent_job_id)
        detail: dict[str, str] = {"spawned_job_id": spawned or ""}
        if denied:
            detail["denied_scopes"] = ";".join(denied)
        return JobInfo(
            channel=self._channel,
            mode=self._mode.value,
            scope=self.scope,
            status=JobStatus.SUCCEEDED,
            detail=detail,
        )


class DreamingCoordinator:
    """dreaming 调度服务（API 层）——注册 / 注销 / 恢复 / 授权裁决，一簿账。

    只持注册表、Scheduler、CommandService、鉴权与 scope 枚举窄端口，不反向
    持有完整 LocalMemoryAPI。调度语义：

    - **注册**（dreaming=True）：调度器能力校验 → submit 驱动 Job → 注册表落盘
      （含注册者 ``created_by``——持续授权锚点）；
    - **注销**（dreaming=False）：幂等——注册表删除 + scheduler.cancel（不命中
      返回 None，不退化为立即跑）；
    - **恢复**（重启）：单实例锁 → 全量加载 → 逐条以 ``created_by`` 复验 →
      复验通过的重新提交（换绑新 job_id）；拒绝的注销（权限已撤，不复跑）。
    """

    def __init__(
        self,
        api=None,
        registry: DreamingRegistry | None = None,
        *,
        scheduler: Scheduler | None = None,
        commands: Any = None,
        authorize: Callable[..., None] | None = None,
        ensure_space_writable: Callable[[Scope], None] | None = None,
        scope_supplier: Callable[[], list[Scope]] | None = None,
    ) -> None:
        # api 参数仅作内部兼容；正式装配传窄端口，避免长命 Job 反向持有整个
        # LocalMemoryAPI 及其全部组件。
        if api is not None:
            scheduler = scheduler or api._scheduler
            commands = commands or api._commands
            authorize = authorize or api._authorize
            ensure_space_writable = ensure_space_writable or api._ensure_space_writable
            scope_supplier = scope_supplier or getattr(
                api._engine, "candidate_scopes", api._engine.kv.scopes
            )
            registry = registry or DreamingRegistry(api._engine.kv)
        required = (
            scheduler,
            commands,
            authorize,
            ensure_space_writable,
            scope_supplier,
            registry,
        )
        if any(port is None for port in required):
            raise TypeError("DreamingCoordinator requires all narrow runtime ports")
        self._scheduler = scheduler
        self._commands = commands
        self._authorize = authorize
        self._ensure_space_writable = ensure_space_writable
        self._scope_supplier = scope_supplier
        self._registry = registry
        self._active_jobs: dict[str, str] = {}
        self._mutation_lock = threading.RLock()
        # restore 幂等缓存只在当前 leader 任期内有效；失锁或主动
        # 释放后必须清空，使下一任期重扫共享注册表。
        self._restored: list[str] | None = None
        # 回调可由续租看守线程触发；先建好上述状态与锁，
        # 再将 self 暴露给 registry。
        self._registry.set_lock_lost_callback(self._on_lock_lost)

    def _registration_key(self, scope: Scope, mode: EvolveMode | str) -> str:
        value = mode.value if isinstance(mode, EvolveMode) else str(mode)
        return self._registry.key_of(scope, value)

    def registration_key(self, scope: Scope, mode: EvolveMode | str) -> str:
        """调度器与注册表共用的无歧义任务键。"""
        return self._registration_key(scope, mode)

    @contextmanager
    def _serialize_mutation(self, scope: Scope, mode: EvolveMode | str):
        del scope, mode
        with self._mutation_lock:
            yield

    def _ensure_leader(self) -> None:
        if self._registry.owns_instance_lock:
            return
        if not self._registry.acquire_instance_lock():
            raise RuntimeError(
                "dreaming 实例锁被其他实例持有：当前实例不是 leader，拒绝操作"
            )

    def check_leadership(self) -> None:
        if not self._registry.owns_instance_lock:
            raise RecurringJobStoppedError(
                "leadership_lost",
                "dreaming leader 租约已丢失，停止周期任务以避免多实例双跑",
            )

    def _stop_local_jobs_locked(self) -> None:
        """废弃当前 leader 任期并停掉本实例的全部周期驱动。"""
        self._restored = None
        job_ids = list(self._active_jobs.values())
        self._active_jobs.clear()
        for job_id in job_ids:
            try:
                self._scheduler.cancel(job_id)
            except Exception:
                logger.exception("failed to stop dreaming timer after lock loss: %s", job_id)

    def _on_lock_lost(self) -> None:
        """租约续租失败时与注册/注销/恢复串行地停掉周期驱动。"""
        with self._mutation_lock:
            self._stop_local_jobs_locked()

    # ---- 授权（入口 / 持续 / 恢复复用同一套，判定不漂移） ------------------ #

    def check_authorization(self, actor: Scope, scope: Scope, mode: EvolveMode | str) -> None:
        """以 actor 复验演进授权：WRITE + 演进空间动作 + 空间可写。

        mode 取值无法解析（存量任务记录的自由字符串）时按 WRITE 基础动作判——
        与 :func:`~api.memory_api_impl.local_support._evolve_space_action` 同口径。
        拒绝抛 :class:`PermissionDeniedError`。同步方法——调用方（tick 的
        Scheduler 事件循环）直接调用，不得引入 ``asyncio.run``。
        """
        try:
            mode_value: EvolveMode | str = EvolveMode(mode)
        except ValueError:
            mode_value = mode
        self._authorize(
            actor,
            scope,
            Action.WRITE,
            "evolve",
            space_action=_evolve_space_action(mode_value),
        )
        self._ensure_space_writable(scope)

    def authorize_fan_out(
        self,
        actor: Scope,
        scope: Scope,
        candidate: CandidateSource | dict | None,
        mode: EvolveMode,
    ) -> tuple[list[Scope] | None, list[str] | None]:
        """fan-out 候选源的逐桶裁决：枚举命中桶 → 逐桶鉴权 → (获准桶, 拒绝标签)。

        非 fan-out 候选源返回 ``(None, None)``——Engine 按内核直调路径自行取数，
        无逐桶语义。枚举与 ``FanOutResolver`` 同源（候选数据面 ``scopes()`` ×
        :func:`covered_by` 前缀 + 形状约束）：``scopes()`` 只枚举有数据的桶，
        裁决时刻与执行时刻之间新建的桶不在获准列表里（fail-closed，下一 tick
        自然纳入）。

        拒绝桶只跳过不中断（denied_scopes 回显）；全拒时仍提交（空桶集 +
        完整拒绝标签）——单链执行不因空集分叉，拒绝信息随 JobInfo 回显。
        """
        fan_out = _parse_fan_out(candidate)
        if fan_out is None:
            return None, None
        hit: list[Scope] = []
        denied_count = 0
        matched_count = 0
        for child in self._scope_supplier():
            if not covered_by(
                scope,
                child,
                require_empty=fan_out.require_empty,
                require_nonempty=fan_out.require_nonempty,
            ):
                continue
            matched_count += 1
            if matched_count > MAX_FAN_OUT_BUCKETS:
                raise ValidationError(
                    "fan_out 命中桶数超过单次上限 "
                    f"{MAX_FAN_OUT_BUCKETS}；请收窄父 scope 或形状约束"
                )
            try:
                self.check_authorization(actor, child, mode)
            except (PermissionDeniedError, ValidationError):
                # 不把调用者无权读取的真实 scope 名称写进用户可见 JobInfo，
                # 否则 fan-out 可被用来枚举资源存在性。
                denied_count += 1
                continue
            hit.append(child)
        return hit, ([f"count:{denied_count}"] if denied_count else None)

    # ---- 三态编排（query_ops.evolve 的执行件） ---------------------------- #

    def register(
        self,
        *,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel,
        candidate: CandidateSource | dict | None,
        interval: int,
        actor: Scope,
    ) -> str:
        """注册定时演进（dreaming=True）：能力校验 → submit → 注册表落盘。

        顺序：调度器能力（``supports_recurring``）→ ``validate``（interval <
        tick_interval 拒绝）→ submit（拿到 job_id）→ 落盘。校验全部在副作用
        （落盘）之前——「注册了但任务拒绝」不产生残留注册；落盘失败回滚
        ``cancel``（F04 D8 写序）——不留「定时器在跑、注册表无记录」的
        注销不掉幽灵任务。
        """
        scheduler = self._scheduler
        if not scheduler.supports_recurring:
            raise ValidationError(
                "dreaming 注册要求支持周期任务的调度器（如 async_timer）；"
                "当前装配的调度器不支持，注册被拒绝"
            )
        dsl = _candidate_dsl(candidate)
        with self._serialize_mutation(scope, mode):
            self._ensure_leader()
            old_entry = self._registry.find(scope, mode.value)
            job = DreamingDriverJob(
                self,
                scope=scope,
                mode=mode,
                channel=channel,
                candidate=dsl,
                actor=actor,
                interval=interval,
            )
            scheduler.validate(job)
            job_id = asyncio.run(scheduler.submit(job, channel))
            try:
                self.check_leadership()
                self._registry.save(
                    DreamingEntry(
                        scope=scope,
                        mode=mode.value,
                        interval=interval,
                        candidate=dsl,
                        job_id=job_id,
                        channel=channel.value,
                        created_by=actor_to_dict(actor),
                    )
                )
                # save 可能阻塞到租约过期；落盘后再验一次，防止
                # 失锁实例把新 timer 当作成功注册留在本地。
                self.check_leadership()
            except RecurringJobStoppedError:
                scheduler.cancel(job_id)
                self._stop_local_jobs_locked()
                raise
            except Exception:
                if old_entry is None:
                    scheduler.cancel(job_id)
                elif self._registry.owns_instance_lock:
                    old_actor = actor_from_dict(old_entry.created_by)
                    if old_actor is not None:
                        try:
                            old_job = DreamingDriverJob(
                                self,
                                scope=old_entry.scope,
                                mode=EvolveMode(old_entry.mode),
                                channel=Channel(old_entry.channel),
                                candidate=old_entry.candidate,
                                actor=old_actor,
                                interval=old_entry.interval,
                            )
                            asyncio.run(
                                scheduler.submit(old_job, Channel(old_entry.channel))
                            )
                        except Exception:
                            logger.exception(
                                "dreaming register rollback could not restore previous timer"
                            )
                raise
            self._active_jobs[self._registration_key(scope, mode)] = job_id
            return job_id

    def unregister(self, scope: Scope, mode: EvolveMode) -> DreamingEntry | None:
        """注销定时演进（dreaming=False，幂等）：注册表删除 + 调度器取消。

        未注册返回 None——**不退化为立即执行**（立即跑只能由 dreaming=None
        明确触发，F04 D2）。取消跨重启换绑前的旧 job_id 是无害 no-op
        （调度器查无此任务直接返回）。
        """
        with self._serialize_mutation(scope, mode):
            self._ensure_leader()
            entry = self._registry.remove(scope, mode.value)
            if entry is not None:
                self._scheduler.cancel(entry.job_id)
                self._active_jobs.pop(self._registration_key(scope, mode), None)
            return entry

    def remove_registration(self, scope: Scope, mode: EvolveMode) -> None:
        """tick 复验拒绝路径的注销（不做调度器取消——父定时器由 PermissionDeniedError 停）。"""
        with self._serialize_mutation(scope, mode):
            if self._registry.owns_instance_lock:
                self._registry.remove(scope, mode.value)
            self._active_jobs.pop(self._registration_key(scope, mode), None)

    async def submit_evolve(
        self,
        *,
        scope: Scope,
        mode: EvolveMode,
        channel: Channel,
        candidate: CandidateSource | dict | None,
        buckets: list[Scope] | None,
        denied_scopes: list[str] | None,
    ) -> str | None:
        """提交一次性 EvolveJob（tick 驱动与立即路径共用件，纯执行链）。"""
        return await self._commands.evolve(
            scope,
            mode,
            channel,
            candidate=candidate,
            buckets=buckets,
            denied_scopes=denied_scopes,
        )

    def link_spawned_job(self, job_id: str, parent_job_id: str) -> None:
        self._scheduler.link_child(job_id, parent_job_id)

    # ---- 重启恢复（F04 D8 单实例部署模型） -------------------------------- #

    def restore(self) -> list[str]:
        """串行化恢复与在线注册/注销，避免同一声明被交叉换绑。"""
        with self._mutation_lock:
            return self._restore_locked()

    def _restore_locked(self) -> list[str]:
        """重启恢复注册态任务：单实例锁 → 逐条复验 → 重新提交（换绑 job_id）。

        锁被他人持有（未过期）抛 :class:`RuntimeError`——部署侧须感知"另一
        实例在跑"。逐条处理：

        - ``created_by`` 缺失（旧版本 entry）→ 跳过（fail-closed，不允许无主
          定时任务复跑），记录保留；
        - mode / channel 取值损坏 → 跳过并告警，记录保留（装配修复后下次
          重启可恢复）；
        - 复验拒绝（权限已撤）→ 注销（不删记录外的任何东西）——注册表不再
          让它在下次重启复活；
        - 复验通过 → 重新提交驱动 Job，注册表换绑新 job_id（旧 id 随旧进程
          消亡，注销必须取消的是新 id）。

        幂等：二次调用返回首次结果，不重扫注册表（恢复后注册表的真相已由
        本协调器维护）。
        """
        if self._restored is not None:
            return self._restored
        scheduler = self._scheduler
        entries = self._registry.load_all()
        if not scheduler.supports_recurring and not entries:
            # 不支持周期任务的短命/同步 surface 无需竞选 leader。
            self._restored = []
            return []
        if not scheduler.supports_recurring:
            raise ValidationError(
                "dreaming 恢复要求支持周期任务的调度器（如 async_timer）；"
                "当前装配的调度器不支持"
            )
        self._ensure_leader()
        # 必须在持锁后重新读取，避免 load → acquire 窗口漏掉并发注册。
        entries = self._registry.load_all()
        restored: list[str] = []
        for entry in entries:
            try:
                self.check_leadership()
            except RecurringJobStoppedError:
                self._stop_local_jobs_locked()
                raise
            job_id: str | None = None
            try:
                actor = actor_from_dict(entry.created_by)
                if actor is None:
                    logger.warning(
                        "dreaming restore skip legacy entry without created_by: "
                        "scope=%s mode=%s", _scope_label(entry.scope), entry.mode,
                    )
                    continue
                mode = EvolveMode(entry.mode)
                channel = Channel(entry.channel)
            except (TypeError, ValueError):
                logger.warning(
                    "dreaming restore skip entry with bad actor/mode/channel: "
                    "scope=%s mode=%s channel=%s",
                    _scope_label(entry.scope), entry.mode, entry.channel,
                )
                continue
            try:
                self.check_authorization(actor, entry.scope, entry.mode)
            except PermissionDeniedError:
                self._registry.remove(entry.scope, entry.mode)
                logger.warning(
                    "dreaming restore unregister revoked entry: "
                    "scope=%s mode=%s", _scope_label(entry.scope), entry.mode,
                )
                continue
            except ValidationError:
                self._registry.remove(entry.scope, entry.mode)
                logger.warning(
                    "dreaming restore unregister non-writable entry: "
                    "scope=%s mode=%s", _scope_label(entry.scope), entry.mode,
                )
                continue
            try:
                candidate = _candidate_dsl(entry.candidate)
                job = DreamingDriverJob(
                    self,
                    scope=entry.scope,
                    mode=mode,
                    channel=channel,
                    candidate=candidate,
                    actor=actor,
                    interval=entry.interval,
                )
                scheduler.validate(job)
                job_id = asyncio.run(scheduler.submit(job, channel))
                self.check_leadership()
                self._registry.save(replace(entry, job_id=job_id))
                self.check_leadership()
            except RecurringJobStoppedError:
                if job_id is not None:
                    scheduler.cancel(job_id)
                self._stop_local_jobs_locked()
                raise
            except Exception as exc:
                # 同 register 写序（F04 D8）：换绑落盘失败回滚 cancel——否则
                # 注册表留着旧 job_id，注销 cancel 旧 id 是 no-op，新定时器成幽灵。entry
                # 保留旧值（与损坏记录同待遇），装配修复后下次重启可恢复。
                if job_id is not None:
                    scheduler.cancel(job_id)
                logger.warning(
                    "dreaming restore skip invalid/failed entry: "
                    "scope=%s mode=%s error=%s: %s",
                    _scope_label(entry.scope), entry.mode, type(exc).__name__, exc,
                )
                continue
            assert job_id is not None
            restored.append(job_id)
            self._active_jobs[self._registration_key(entry.scope, entry.mode)] = job_id
        try:
            self.check_leadership()
        except RecurringJobStoppedError:
            self._stop_local_jobs_locked()
            raise
        self._restored = restored
        return restored

    def release_lock(self) -> None:
        """释放恢复实例锁（优雅关闭路径）。幂等——未持锁 / 锁已易主均无害。"""
        with self._mutation_lock:
            try:
                self._registry.release_instance_lock()
            finally:
                # 主动释放不会触发续租失败回调；在同一串行区间
                # 主动结束本地任期，避免旧 timer 在放锁后继续跑。
                self._stop_local_jobs_locked()
