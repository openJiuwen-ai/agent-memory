# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""WriteOrchestrator 的分组、并发、锁段与失败归集（F10 决策 3、7、8）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from jiuwen_memory.common.lock import LockTimeoutError
from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem, BatchWriteOutcome
from jiuwen_memory.control.write_orchestrator import WriteTask
from jiuwen_memory.control.write_orchestrator_impl.grouped import GroupedWriteOrchestrator
from jiuwen_memory.control.write_orchestrator_impl.sequential import SequentialWriteOrchestrator

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="u1")


def _item(content: str, *, scope: Scope | None = None, stream: str = "", infer: bool = True):
    return BatchWriteItem(
        content=content,
        scope=scope or _SCOPE,
        stream_id=stream,
        system_metadata={"infer": "true"} if infer else {},
    )


class _Engine:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.calls: list[list[str]] = []
        self.evolves: list[list[str]] = []
        self.max_active = 0
        self._active = 0

    @staticmethod
    def supports_grouped_write(items) -> bool:
        return True

    async def write_group(self, items, *, merge_extract: bool):
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            await asyncio.sleep(0.01)
            self.calls.append([item.content for item in items])
            if merge_extract:
                self.evolves.append([item.content for item in items])
            else:
                self.evolves.extend([[item.content] for item in items])
            outcomes = []
            for index, item in enumerate(items):
                outcome = BatchWriteOutcome(index=index, item=item, units=[])
                if item.content in self.fail_on:
                    outcome.error = "boom"
                    outcome.error_type = "RuntimeError"
                outcomes.append(outcome)
            return outcomes
        finally:
            self._active -= 1


def _grouped(**kwargs) -> GroupedWriteOrchestrator:
    return GroupedWriteOrchestrator(
        lock=InMemoryLockProvider(wait_timeout_ms=5_000), **kwargs
    )


def test_grouped_batches_by_stream() -> None:
    engine = _Engine()
    orchestrator = _grouped()
    task = WriteTask(
        items=[_item("a1", stream="s1"), _item("b1", stream="s2"), _item("a2", stream="s1")]
    )

    result = asyncio.run(orchestrator.write(engine, task))

    assert sorted(engine.calls) == [["a1", "a2"], ["b1"]], "同 stream 成组、组内保序"
    assert [outcome.index for outcome in result.outcomes] == [0, 1, 2]


def test_sequential_executes_in_input_order() -> None:
    engine = _Engine()
    orchestrator = SequentialWriteOrchestrator(lock=InMemoryLockProvider())

    asyncio.run(orchestrator.write(engine, WriteTask(items=[_item("x"), _item("y")])))

    assert engine.calls == [["x"], ["y"]], "兼容模式逐条按输入顺序执行"


def test_same_key_from_two_tasks_serialize() -> None:
    engine = _Engine()
    orchestrator = _grouped()

    async def scenario() -> None:
        await asyncio.gather(
            orchestrator.write(engine, WriteTask(items=[_item("first")])),
            orchestrator.write(engine, WriteTask(items=[_item("second")])),
        )

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert engine.max_active == 1, "同一用户的并发请求必须被锁串行化"


def test_multi_user_interleaved_no_deadlock() -> None:
    engine = _Engine()
    orchestrator = _grouped()
    users = [Scope(org="acme", space="", user=f"u{i}") for i in range(3)]
    items = [
        _item("u0-a", scope=users[0]),
        _item("u1-a", scope=users[1]),
        _item("u0-b", scope=users[0]),
        _item("u2-a", scope=users[2]),
        _item("u1-b", scope=users[1]),
    ]

    result = asyncio.run(
        asyncio.wait_for(orchestrator.write(engine, WriteTask(items=items)), timeout=10)
    )

    assert len(result.outcomes) == 5, "混合用户的批次必须全部返回，且不死锁"
    assert sorted(engine.calls) == [["u0-a", "u0-b"], ["u1-a", "u1-b"], ["u2-a"]], (
        "不同用户的段各自成批"
    )


def test_lock_released_on_failure() -> None:
    engine = _Engine(fail_on={"bad"})
    provider = InMemoryLockProvider(wait_timeout_ms=0)
    orchestrator = GroupedWriteOrchestrator(lock=provider)

    result = asyncio.run(orchestrator.write(engine, WriteTask(items=[_item("bad")])))

    assert result.outcomes[0].error, "引擎失败必须归集到该条"
    handle = asyncio.run(provider.acquire(_SCOPE, "infer_write", wait_timeout_ms=0))
    assert handle is not None, "失败后锁必须已经释放"


def test_lock_segment_split_by_direct_write() -> None:
    engine = _Engine()
    orchestrator = _grouped()
    items = [
        _item("infer-1"),
        _item("direct", infer=False),
        _item("infer-2"),
    ]

    asyncio.run(orchestrator.write(engine, WriteTask(items=items)))

    assert engine.calls == [["infer-1"], ["direct"], ["infer-2"]], (
        "直写条目把 infer 段切开，两段各自取锁"
    )


def test_max_items_one_equals_merge_off() -> None:
    engine_chunked = _Engine()
    asyncio.run(
        _grouped(max_items=1).write(
            engine_chunked, WriteTask(items=[_item("n1"), _item("n2")], merge_extract=True)
        )
    )

    engine_off = _Engine()
    asyncio.run(
        _grouped().write(
            engine_off, WriteTask(items=[_item("n1"), _item("n2")], merge_extract=False)
        )
    )

    assert engine_chunked.evolves == engine_off.evolves == [["n1"], ["n2"]], (
        "max_items=1 与关闭合并的抽取粒度必须一致"
    )


def test_continue_on_error_false_stops_and_skips() -> None:
    engine = _Engine(fail_on={"b"})
    orchestrator = _grouped()

    result = asyncio.run(
        orchestrator.write(
            engine,
            WriteTask(
                items=[_item("a"), _item("b"), _item("c")],
                continue_on_error=False,
            ),
        )
    )

    assert engine.calls == [["a"], ["b"]], "fail-fast：失败后不再执行后续条目"
    assert result.outcomes[2].error_type == "Skipped", "未执行条目必须标为 Skipped"


def test_slow_engine_does_not_block_orchestrator_lock_release() -> None:
    class _SlowEngine(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            await asyncio.sleep(0.2)
            return await super().write_group(items, merge_extract=merge_extract)

    provider = InMemoryLockProvider(wait_timeout_ms=5_000)
    orchestrator = GroupedWriteOrchestrator(lock=provider)

    async def scenario() -> float:
        started = time.monotonic()
        await orchestrator.write(_SlowEngine(), WriteTask(items=[_item("slow")]))
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed >= 0.2, "锁的等待与释放不得跳过实际写入"


class _NamedEngine(_Engine):
    """按 content 精确控制失败与调用记录。"""


def test_sequential_direct_items_stop_after_failure() -> None:
    engine = _Engine(fail_on={"b"})
    orchestrator = SequentialWriteOrchestrator(lock=InMemoryLockProvider())

    result = asyncio.run(
        orchestrator.write(
            engine,
            WriteTask(
                items=[_item("a", infer=False), _item("b", infer=False), _item("c", infer=False)],
                continue_on_error=False,
            ),
        )
    )

    assert engine.calls == [["a"], ["b"]], "直写段也必须逐项提交，失败后不再写 c"
    assert result.outcomes[2].error_type == "Skipped", "未执行条目必须标为 Skipped"


def test_sequential_lock_timeout_marks_items_without_raising() -> None:
    provider = InMemoryLockProvider(wait_timeout_ms=0)
    orchestrator = SequentialWriteOrchestrator(lock=provider, lock_wait_ms=0)
    engine = _Engine()

    async def scenario() -> tuple[list, bool]:
        handle = await provider.acquire(_SCOPE, "infer_write", wait_timeout_ms=0)

        async def run() -> list:
            # 另一个 task 的 context 才不是重入；同上下文会被当成重入直接放行。
            result = await orchestrator.write(
                engine,
                WriteTask(
                    items=[_item("blocked"), _item("other", scope=Scope(org="acme", user="u2"))]
                ),
            )
            return result.outcomes

        try:
            return await asyncio.create_task(run()), handle is not None
        finally:
            await provider.release(handle)

    outcomes, held = asyncio.run(scenario())

    assert held, "测试前提：锁被外部持有"
    assert outcomes[0].error_type == "LockTimeoutError", "锁超时归到具体条目"
    assert not outcomes[1].error, "其他用户的分段不受影响，continue_on_error=True 继续"


def test_llm_lane_cannot_acquire_write_lock() -> None:
    from jiuwen_memory.control.executor import ExecutorLane
    from jiuwen_memory.control.executor_impl.thread_pool import ThreadPoolBlockingExecutor

    executor = ThreadPoolBlockingExecutor(llm_workers=1, io_workers=1)
    provider = InMemoryLockProvider(wait_timeout_ms=0)
    orchestrator = GroupedWriteOrchestrator(
        lock=provider, executor=executor, lock_wait_ms=0
    )
    engine = _Engine()

    async def scenario():
        async def run_in_llm_lane():
            return await orchestrator.write(engine, WriteTask(items=[_item("infer")]))

        result = await executor.run(ExecutorLane.LLM, lambda: asyncio.run(run_in_llm_lane()))
        return result

    result = asyncio.run(scenario())
    handle = asyncio.run(provider.acquire(_SCOPE, "infer_write", wait_timeout_ms=0))

    assert result.outcomes[0].error_type == "ValidationError", "LLM 工作线程取锁必须被拒绝"
    assert engine.calls == [], "被拒绝时不得进入临界区"
    assert handle is not None, "拒绝路径不能留下未释放的锁"
    asyncio.run(provider.release(handle))
    executor.close(wait=True)


def test_short_lease_is_renewed_during_long_write() -> None:
    """写入时长超过租约时，续租必须生效；结束后锁必须释放。"""
    provider = InMemoryLockProvider(lease_ms=50, wait_timeout_ms=0)
    orchestrator = GroupedWriteOrchestrator(lock=provider, lock_wait_ms=0)

    class _SlowEngine(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            await asyncio.sleep(0.3)  # 6 个租约周期
            return await super().write_group(items, merge_extract=merge_extract)

    async def try_acquire() -> bool:
        try:
            handle = await provider.acquire(_SCOPE, "infer_write", wait_timeout_ms=0)
        except LockTimeoutError:
            return False
        await provider.release(handle)
        return True

    async def scenario() -> tuple[bool, bool]:
        task = asyncio.create_task(
            orchestrator.write(_SlowEngine(), WriteTask(items=[_item("slow")]))
        )
        await asyncio.sleep(0.15)
        blocked = await asyncio.create_task(try_acquire())
        await task
        released = await asyncio.create_task(try_acquire())
        return blocked, released

    blocked, released = asyncio.run(scenario())

    assert blocked is False, "租约到期前必须已续租，否则第二个写会并发进入临界区"
    assert released is True, "写入结束后锁必须释放"


def test_non_domain_engine_failure_is_sanitized(caplog) -> None:
    import logging

    class _BoomEngine(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            raise RuntimeError("secret internal path /tmp/x")

    logger_name = "agent_memory.control.write_orchestrator_impl.grouped"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        result = asyncio.run(_grouped().write(_BoomEngine(), WriteTask(items=[_item("x")])))

    outcome = result.outcomes[0]
    assert outcome.error_type == "InternalError", "非领域异常按内部错误报告"
    assert "secret" not in outcome.error, "内部异常原文不得进入回执"
    assert isinstance(outcome.error_obj, RuntimeError), "原始异常保留给内部诊断"
    assert any("write segment failed" in record.message for record in caplog.records), (
        "非领域异常必须留下日志现场"
    )


def test_lock_backend_failure_is_reported_by_health() -> None:
    from jiuwen_memory.common.errors import BackendError

    class _BrokenLock(InMemoryLockProvider):
        async def _acquire(self, key, *, lease_ms, wait_timeout_ms):
            raise BackendError("redis down")

    orchestrator = GroupedWriteOrchestrator(lock=_BrokenLock(wait_timeout_ms=0))

    result = asyncio.run(orchestrator.write(_Engine(), WriteTask(items=[_item("x")])))

    assert result.outcomes[0].error_type == "BackendError"
    with pytest.raises(BackendError, match="redis down"):
        orchestrator.health()


def test_engine_without_grouped_write_records_degraded_signal() -> None:
    class _LegacyEngine(_Engine):
        @staticmethod
        def supports_grouped_write(items) -> bool:
            return False

    orchestrator = _grouped()

    asyncio.run(
        orchestrator.write(_LegacyEngine(), WriteTask(items=[_item("x"), _item("y")]))
    )

    assert orchestrator.degrade_counters().get("engine_degraded") == 1, (
        "引擎退化为逐条时必须留下可观测信号"
    )


def test_legacy_engine_single_item_records_no_signal() -> None:
    class _LegacyEngine(_Engine):
        @staticmethod
        def supports_grouped_write(items) -> bool:
            return False

    orchestrator = _grouped()

    asyncio.run(orchestrator.write(_LegacyEngine(), WriteTask(items=[_item("x")])))

    assert orchestrator.degrade_counters() == {}, "单条写入不存在合并，不算降级"


def test_degrade_counters_are_instance_scoped() -> None:
    first = _grouped()
    second = _grouped()

    class _BoomEngine(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            raise RuntimeError("boom")

    asyncio.run(first.write(_BoomEngine(), WriteTask(items=[_item("x")])))

    assert second.degrade_counters() == {}, "计数器不得跨实例共享"


def test_health_recovers_after_lock_backend_returns() -> None:
    from jiuwen_memory.common.errors import BackendError

    state = {"broken": True}

    class _FlakyLock(InMemoryLockProvider):
        async def _acquire(self, key, *, lease_ms, wait_timeout_ms):
            if state["broken"]:
                raise BackendError("redis down")
            return await super()._acquire(
                key, lease_ms=lease_ms, wait_timeout_ms=wait_timeout_ms
            )

    orchestrator = GroupedWriteOrchestrator(lock=_FlakyLock(wait_timeout_ms=0), lock_wait_ms=0)
    asyncio.run(orchestrator.write(_Engine(), WriteTask(items=[_item("first")])))
    with pytest.raises(BackendError):
        orchestrator.health()

    state["broken"] = False
    result = asyncio.run(orchestrator.write(_Engine(), WriteTask(items=[_item("second")])))

    assert not result.outcomes[0].error, "后端恢复后写入必须成功"
    orchestrator.health()  # 不抛：故障状态必须能被后续成功取锁清除


def test_storage_backend_error_does_not_pollute_lock_health() -> None:
    from jiuwen_memory.common.errors import BackendError

    class _StorageFailure(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            raise BackendError("kv down")

    orchestrator = _grouped()
    result = asyncio.run(orchestrator.write(_StorageFailure(), WriteTask(items=[_item("x")])))

    assert result.outcomes[0].error_type == "BackendError", "存储故障按原类型报告给条目"
    orchestrator.health()  # 不抛：存储故障不是锁故障


def test_cancellation_waits_for_other_groups() -> None:
    """一组取消时，另一组必须先把在飞的写入做完，异常之后才抛出。"""
    from jiuwen_memory.common.ingest_cancellation import IngestCancelled

    finished: list[str] = []

    class _CancelEngine(_Engine):
        async def write_group(self, items, *, merge_extract: bool):
            user = items[0].scope.user
            if user == "u-cancel":
                await asyncio.sleep(0.05)
                raise IngestCancelled("stop")
            await asyncio.sleep(0.25)
            finished.append(user)
            return await super().write_group(items, merge_extract=merge_extract)

    cancel_scope = Scope(org="acme", space="", user="u-cancel")
    slow_scope = Scope(org="acme", space="", user="u-slow")
    task = WriteTask(items=[_item("a", scope=cancel_scope), _item("b", scope=slow_scope)])

    started = time.monotonic()
    with pytest.raises(IngestCancelled):
        asyncio.run(_grouped().write(_CancelEngine(), task))
    elapsed = time.monotonic() - started

    assert finished == ["u-slow"], "另一组取消不能打断这一组的写入"
    assert elapsed >= 0.25, "异常必须在所有组结束后才抛出"


def test_release_failure_keeps_committed_outcome_and_recovers(monkeypatch) -> None:
    """释放锁失败不能把已提交的条目标成失败；health 报错并在下次成功取锁后恢复。"""
    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.control.write_orchestrator_impl import grouped as grouped_module

    state = {"break_release": True}

    class _BadReleaseLock(InMemoryLockProvider):
        async def _release(self, handle):
            if state["break_release"]:
                raise BackendError("release failed")
            await super()._release(handle)

    logged: list[tuple] = []
    monkeypatch.setattr(
        grouped_module.logger, "exception", lambda *args, **kwargs: logged.append(args)
    )
    orchestrator = GroupedWriteOrchestrator(
        lock=_BadReleaseLock(lease_ms=50, wait_timeout_ms=0)
    )

    first = asyncio.run(orchestrator.write(_Engine(), WriteTask(items=[_item("first")])))

    assert not first.outcomes[0].error, "写入已提交，释放锁失败不得改写回执"
    with pytest.raises(BackendError):
        orchestrator.health()
    assert logged, "释放失败必须留 ERROR 日志"

    state["break_release"] = False
    asyncio.run(asyncio.sleep(0.1))  # 等租约到期：释放失败靠租约回收
    second = asyncio.run(orchestrator.write(_Engine(), WriteTask(items=[_item("second")])))

    assert not second.outcomes[0].error
    orchestrator.health()  # 下一次取锁成功后恢复健康
