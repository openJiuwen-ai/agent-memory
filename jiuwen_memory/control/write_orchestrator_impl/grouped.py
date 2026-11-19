# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``grouped`` 编排：分组 → 切批 → 加锁 → 组间有界并发（F10 决策 2、8）。

锁段与切批规则见 :mod:`segments`。本模块只负责调度与失败归集：不鉴权、不审计、
不调用 LLM。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from jiuwen_memory.common.errors import AgentMemoryError, BackendError, ValidationError
from jiuwen_memory.common.ingest_cancellation import IngestCancelled
from jiuwen_memory.common.lock import LockProvider, LockTimeoutError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.executor import ExecutorLane
from jiuwen_memory.control.types import BatchWriteItem, BatchWriteOutcome, BatchWriteResult
from jiuwen_memory.control.write_orchestrator_impl.segments import (
    Entry,
    chunk_entries,
    executor_from_config,
    group_key,
    lock_scope,
    split_runs,
    validate_lock_scope,
)
from jiuwen_memory.control.write_orchestrator_impl.signals import (
    DegradeEvent,
    WriteSignals,
)

from ..base import ControlOperatorType
from ..write_orchestrator import WriteOrchestrator, WriteOrchestratorProducer, WriteTask

logger = get_logger(__name__)

DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_TOKENS = 6_000
DEFAULT_LOCK_NAME = "infer_write"


class GroupedWriteOrchestrator(WriteOrchestrator):
    """缺省实现：相同 ``(scope, stream_id)`` 成组，组间并发、组内保序。"""

    def __init__(
        self,
        *,
        lock: LockProvider | None,
        executor=None,
        signals: WriteSignals | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        lock_name: str = DEFAULT_LOCK_NAME,
        lock_wait_ms: int | None = None,
    ) -> None:
        for name, value in (
            ("concurrency", concurrency),
            ("max_items", max_items),
            ("max_tokens", max_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError(f"write orchestrator {name} must be a positive int")
        if lock is None:
            raise ValidationError(
                "write.orchestrator=grouped 需要 per-user 锁：请配置 lock.default"
            )
        self._lock = lock
        self._executor = executor
        self._signals = signals or WriteSignals()
        self._last_lock_error: Exception | None = None
        self._concurrency = concurrency
        self._max_items = max_items
        self._max_tokens = max_tokens
        self._lock_name = lock_name
        self._lock_wait_ms = lock_wait_ms

    # -- 算子契约 ------------------------------------------------------------ #

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.WRITE_ORCHESTRATOR

    def bind_signals(self, signals: WriteSignals) -> None:
        """装配期绑定共享计数器：引擎与编排器共写同一份，health 可查到全链路降级。"""
        self._signals = signals

    def degrade_counters(self) -> dict[str, int]:
        """当前降级计数快照（S09 可观测性：从 health 侧可查）。"""
        return self._signals.counters()

    def health(self) -> None:
        """锁的 ``health()`` 是异步契约，这里不 await；改报最近一次锁后端故障。

        装配期已校验锁的互斥范围；运行期后端不可用（``BackendError``）会让后续写入
        持续失败，此时 ``health`` 必须能报出来，而不是恒返回 None。
        """
        if isinstance(self._last_lock_error, BackendError):
            raise self._last_lock_error

    # -- 入口 ---------------------------------------------------------------- #

    async def write(self, engine, task: WriteTask) -> BatchWriteResult:
        if not task.items:
            return BatchWriteResult(outcomes=[])
        results: list[BatchWriteOutcome | None] = [None] * len(task.items)
        entries = [Entry(index=index, item=item) for index, item in enumerate(task.items)]
        blocker = self._blocker(engine, task.items)
        supported = blocker is None
        if not supported and task.continue_on_error and len(entries) > 1:
            # 只在「本来会分组合并」时才算降级：continue_on_error=False 与单条写入
            # 本就逐条执行，记信号只会刷屏。事件按引擎报告的真实原因记录。
            self._signals.record(
                _degrade_event(blocker),
                scope=_scope_of(entries),
                batch_size=len(entries),
                reason=f"engine.grouped_write_blocker() = {blocker}",
            )
        if not task.continue_on_error or not supported:
            await self._run_sequential(engine, entries, task, results)
        else:
            await self._run_grouped(engine, entries, task, results)
        return BatchWriteResult(
            outcomes=[
                outcome
                if outcome is not None
                else self._failed(task.items[index], RuntimeError("write outcome missing"))
                for index, outcome in enumerate(results)
            ]
        )

    async def batch_write_aligned(
        self,
        engine,
        items: list[BatchWriteItem],
        origins: list[tuple[int, BatchWriteItem]],
        *,
        merge_extract: bool,
        continue_on_error: bool,
    ) -> list[BatchWriteOutcome]:
        result = await self.write(
            engine,
            WriteTask(
                items=list(items), merge_extract=merge_extract, continue_on_error=continue_on_error
            ),
        )
        aligned: list[BatchWriteOutcome] = []
        for outcome, (index, item) in zip(result.outcomes, origins):
            outcome.index = index
            outcome.item = item
            aligned.append(outcome)
        return aligned

    # -- 分组并发 ------------------------------------------------------------ #

    async def _run_grouped(
        self,
        engine,
        entries: list[Entry],
        task: WriteTask,
        results: list[BatchWriteOutcome | None],
    ) -> None:
        groups: dict[tuple, list[Entry]] = {}
        for entry in entries:
            groups.setdefault(group_key(entry.item), []).append(entry)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def run(group: list[Entry]) -> None:
            async with semaphore:
                await self._run_group(engine, group, task, results)

        # return_exceptions=True：某一组取消时，其余组必须先把在飞的写入做完再抛，
        # 否则「已报告 cancelled」之后还会有写入落库（F10 决策 8）。
        outcomes = await asyncio.gather(
            *(run(group) for group in groups.values()), return_exceptions=True
        )
        errors = [item for item in outcomes if isinstance(item, BaseException)]
        if errors:
            cancellation = next(
                (error for error in errors if isinstance(error, IngestCancelled)), None
            )
            raise cancellation or errors[0]

    async def _run_group(
        self,
        engine,
        entries: list[Entry],
        task: WriteTask,
        results: list[BatchWriteOutcome | None],
    ) -> None:
        for key, run in split_runs(entries):
            chunks = (
                chunk_entries(run, max_items=self._max_items, max_tokens=self._max_tokens)
                if key is not None
                else [run]
            )
            for chunk in chunks:
                outcomes = await self._execute(
                    engine, chunk, merge=task.merge_extract and key is not None, lock_key=key
                )
                self._assign(chunk, outcomes, results)

    # -- 逐条 / 兼容路径 ------------------------------------------------------ #

    async def _run_sequential(
        self,
        engine,
        entries: list[Entry],
        task: WriteTask,
        results: list[BatchWriteOutcome | None],
    ) -> None:
        """真值表第 3、4 行：不分组、不合并、逐条执行；infer 段仍持锁。

        锁覆盖整个连续 infer 段，段内逐条调用引擎；``continue_on_error=False`` 时遇到
        第一个失败即停批，后续条目标 ``Skipped``。
        """
        stop_on_error = not task.continue_on_error
        stopped = False
        for key, run in split_runs(entries):
            if stopped:
                self._mark_skipped(run, results)
                continue
            if key is None:
                # fail-fast 必须逐项提交：整段一次提交时，后面的条目在返回前就已写入，
                # 无法在「上一项失败」后停下来。
                await self._run_items(engine, run, results, stop_on_error)
                stopped = self._stopped(run, results, stop_on_error)
                if stopped:
                    self._mark_skipped(run, results)
                continue
            try:
                self._ensure_lockable()
                async with self._locked(key):
                    for position, entry in enumerate(run):
                        if stopped:
                            break
                        await self._run_items(engine, [entry], results, stop_on_error)
                        stopped = self._stopped([entry], results, stop_on_error)
                    if stopped:
                        self._mark_skipped(run[position:], results)
            except IngestCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 —— 取锁失败按条归集，不冒出批量 API
                if isinstance(exc, LockTimeoutError):
                    self._signals.record(
                        DegradeEvent.LOCK_TIMEOUT,
                        scope=Scope(org=key[0], space=key[1], user=key[2]),
                        batch_size=len(run),
                        reason=type(exc).__name__,
                    )
                self._fail_entries(run, results, exc)
                stopped = stop_on_error

    async def _run_items(
        self,
        engine,
        entries: list[Entry],
        results: list[BatchWriteOutcome | None],
        stop_on_error: bool,
    ) -> None:
        """逐条提交（fail-fast 路径）或整段提交（非 fail-fast 路径）。"""
        if stop_on_error:
            for entry in entries:
                if results[entry.index] is not None:
                    continue
                outcomes = await self._execute(engine, [entry], merge=False, lock_key=None)
                self._assign([entry], outcomes, results)
                if outcomes and outcomes[0].error:
                    # fail-fast：下一项在提交前检查前一项结果。
                    break
            return
        outcomes = await self._execute(engine, entries, merge=False, lock_key=None)
        self._assign(entries, outcomes, results)

    @staticmethod
    def _stopped(
        entries: list[Entry],
        results: list[BatchWriteOutcome | None],
        stop_on_error: bool,
    ) -> bool:
        if not stop_on_error:
            return False
        return any(
            results[entry.index] is not None and results[entry.index].error for entry in entries
        )

    def _fail_entries(
        self,
        entries: list[Entry],
        results: list[BatchWriteOutcome | None],
        exc: Exception,
    ) -> None:
        for entry in entries:
            if results[entry.index] is None:
                outcome = self._failed(entry.item, exc)
                outcome.index = entry.index
                results[entry.index] = outcome

    def _note_lock_error(self, exc: Exception) -> None:
        """只在**取锁/释放锁**路径调用；取锁成功即视为恢复，由 ``_locked`` 清空。"""
        if isinstance(exc, (LockTimeoutError, BackendError)):
            self._last_lock_error = exc

    @asynccontextmanager
    async def _locked(self, key: tuple[str, str, str]):
        """取锁失败/释放失败记账；取到锁即清除历史故障，health 能随后端恢复。"""
        guard = self._lock_guard(key)
        try:
            handle = await guard.__aenter__()
        except Exception as exc:
            self._note_lock_error(exc)
            raise
        self._last_lock_error = None
        try:
            yield handle
        finally:
            try:
                await guard.__aexit__(None, None, None)
            except Exception as exc:
                # 释放失败不能把已提交的条目标成失败：调用方重试会写出重复记忆。
                # 锁靠租约到期回收，这里只记账并留日志。
                self._note_lock_error(exc)
                logger.exception("write lock release failed; rely on lease expiry")

    def _ensure_lockable(self) -> None:
        """LLM 工作线程不能取写锁：它可能正被某个持锁写入等待，取了就互相堵死。"""
        lane = self._executor.current_lane() if self._executor is not None else None
        if lane is ExecutorLane.LLM:
            raise ValidationError(
                "per-user write lock must not be acquired on an llm executor thread"
            )

    # -- 执行与归集 ---------------------------------------------------------- #

    async def _execute(
        self,
        engine,
        chunk: list[Entry],
        *,
        merge: bool,
        lock_key: tuple[str, str, str] | None,
    ) -> list[BatchWriteOutcome]:
        items = [entry.item for entry in chunk]
        try:
            outcomes = await self._guarded(engine, items, merge=merge, lock_key=lock_key)
        except LockTimeoutError as exc:
            # 超时由锁路径记账；这里只补降级信号。
            self._signals.record(
                DegradeEvent.LOCK_TIMEOUT,
                scope=_scope_of(chunk),
                batch_size=len(chunk),
                reason=type(exc).__name__,
            )
            return [self._failed(item, exc) for item in items]
        except IngestCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 —— 失败的段整段归集，不中断其他段
            # 引擎/存储异常不是锁故障：不写 _last_lock_error，只记录现场。
            if not isinstance(exc, AgentMemoryError):
                logger.exception("write segment failed for %d items", len(chunk))
            return [self._failed(item, exc) for item in items]
        for entry, outcome in zip(chunk, outcomes):
            outcome.item = entry.item
        return outcomes

    async def _guarded(
        self,
        engine,
        items: list[BatchWriteItem],
        *,
        merge: bool,
        lock_key: tuple[str, str, str] | None,
    ) -> list[BatchWriteOutcome]:
        if lock_key is None:
            return await self._shielded(engine, items, merge)
        self._ensure_lockable()
        async with self._locked(lock_key):
            return await self._shielded(engine, items, merge)

    def _lock_guard(self, key: tuple[str, str, str]):
        scope = Scope(org=key[0], space=key[1], user=key[2])
        return self._lock.guard(
            scope, self._lock_name, wait_timeout_ms=self._lock_wait_ms
        )

    async def _shielded(self, engine, items: list[BatchWriteItem], merge: bool):
        """取消时先等线程跑完再抛出：锁释放时不能还有写入在悄悄进行（F10 决策 8）。"""
        task = asyncio.create_task(engine.write_group(items, merge_extract=merge))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(BaseException):
                await task
            raise

    def _assign(
        self,
        chunk: list[Entry],
        outcomes: list[BatchWriteOutcome],
        results: list[BatchWriteOutcome | None],
    ) -> None:
        for entry, outcome in zip(chunk, outcomes):
            outcome.index = entry.index
            outcome.item = entry.item
            results[entry.index] = outcome

    def _mark_skipped(
        self, run: list[Entry], results: list[BatchWriteOutcome | None]
    ) -> None:
        for entry in run:
            if results[entry.index] is not None:
                continue
            results[entry.index] = BatchWriteOutcome(
                index=entry.index,
                item=entry.item,
                error="skipped after previous item failed",
                error_type="Skipped",
            )

    @staticmethod
    def _failed(item: BatchWriteItem | None, exc: Exception) -> BatchWriteOutcome:
        """与引擎同口径：领域异常保留类型与消息，其余按内部错误屏蔽原文。"""
        domain = isinstance(exc, AgentMemoryError)
        outcome = BatchWriteOutcome(
            index=-1,
            item=item,
            error=str(exc) if domain else "unexpected write failure",
            error_type=type(exc).__name__ if domain else "InternalError",
        )
        # 原始异常只留作内部诊断（单条入口按领域异常决定是否原样重抛）。
        outcome.error_obj = exc
        return outcome

    @staticmethod
    def _supports(engine, items: list[BatchWriteItem]) -> bool:
        supports = getattr(engine, "supports_grouped_write", None)
        return bool(supports(items)) if callable(supports) else False

    @classmethod
    def _blocker(cls, engine, items: list[BatchWriteItem]) -> str | None:
        explain = getattr(engine, "grouped_write_blocker", None)
        if callable(explain):
            return explain(items)
        return None if cls._supports(engine, items) else DegradeEvent.ENGINE_DEGRADED.value


def _degrade_event(blocker: str | None) -> DegradeEvent:
    try:
        return DegradeEvent(blocker)
    except ValueError:
        return DegradeEvent.ENGINE_DEGRADED


def _scope_of(chunk: list[Entry]) -> Scope | None:
    for entry in chunk:
        if entry.item.scope is not None:
            return lock_scope(entry.item.scope)
    return None


@WriteOrchestratorProducer.register("grouped")
def _build(config):
    from jiuwen_memory.common.factory.factory import Factory
    from jiuwen_memory.common.lock import LockProducer

    lock = LockProducer.dep(config, "lock")
    validate_lock_scope(config.ctx, lock)
    return GroupedWriteOrchestrator(
        lock=lock,
        executor=executor_from_config(config),
        concurrency=int(
            Factory.cfg_get(config, "concurrency", DEFAULT_CONCURRENCY)
        ),
        max_items=int(Factory.cfg_get(config, "max_items", DEFAULT_MAX_ITEMS)),
        max_tokens=int(Factory.cfg_get(config, "max_tokens", DEFAULT_MAX_TOKENS)),
        lock_name=str(Factory.cfg_get(config, "lock_name", DEFAULT_LOCK_NAME)),
        lock_wait_ms=_optional_int(Factory.cfg_get(config, "lock_wait_ms", None)),
    )


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
