"""MiddleToLongJob 单元测试——中期转长期 Job 行为。

覆盖：
- ``_list_working_units``：tier=WORKING + lifecycle=ACTIVE 过滤 + t_ingest 升序 + max_fetch 截断；
- ``_check_continuity``：3 次重试 + 失败默认 "true" + ChatMessage 对象形式入参；
- ``_split_by_continuity``：连续切批 / 不连续切批 / batch_size 上限；
- ``_archive_originals``：调 lifecycle.transition(scope, ids, ARCHIVED) + index.remove(units)；
- ``run``：串行路径 / 并发路径 / 无候选返回 is_done=true / 失败批次原文保留。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Segment,
    Scope,
    Temporal,
    memory_key,
)
from common.type_def.memory_codec import dumps
from construction import EvolveMode, EvolveResult, Evolver
from construction.base import OperatorType
from construction.index_builder import IndexBuilder
from common.base import PluginType
from common.llm.base import LLM
from common.type_def.chat import ChatMessage
from control.base import ControlOperatorType
from control.jobs_impl.middle_to_long_job import MiddleToLongJob
from control.lifecycle import LifecycleManager
from control.types import JobStatus
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


# ---- 测试替身 ----


class _RecordingEvolver(Evolver):
    """记录 evolve 调用入参的 Evolver 替身。"""

    def __init__(self, fail_on_batches: int | None = None) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []
        self._fail_on_batches = fail_on_batches  # 前 N 批抛错

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        if self._fail_on_batches is not None and len(self.calls) <= self._fail_on_batches:
            raise RuntimeError(f"mock evolve fail on batch {len(self.calls)}")
        return EvolveResult(created_ids=[f"created-{u.id}" for u in units])


class _RecordingLifecycle(LifecycleManager):
    """记录 transition 调用入参的 LifecycleManager 替身。"""

    def __init__(self) -> None:
        self.transition_calls: list[tuple[Scope, list[str], LifecycleState]] = []

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.LIFECYCLE

    def health(self) -> None:
        return None

    def transition(
        self, scope: Scope, unit_ids: list[str], target: LifecycleState
    ) -> None:
        self.transition_calls.append((scope, unit_ids, target))

    def supersede(self, scope: Scope, unit_id: str, invalid_at: datetime) -> MemoryUnit:
        raise AssertionError("MiddleToLongJob should not call supersede")

    def sweep(self) -> list[str]:
        return []


class _RecordingIndex(IndexBuilder):
    """记录 remove 调用入参的 IndexBuilder 替身。"""

    def __init__(self) -> None:
        self.removed: list[MemoryUnit] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.INDEX_BUILDER

    def health(self) -> None:
        return None

    def build(self, units) -> None:
        return None

    def update(self, units) -> None:
        return None

    def remove(self, units) -> None:
        self.removed.extend(units)

    def rebuild(self) -> None:
        return None


class _ScriptedLLM(LLM):
    """脚本化 LLM——按预设响应队列返回，记录 chat 入参。

    - 若响应队列中有 Exception，抛出模拟 LLM 失败；
    - 否则返回队首字符串（弹出）。
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.chat_calls: list[list[ChatMessage]] = []

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages: list[ChatMessage], **options: object) -> str:
        self.chat_calls.append(messages)
        if not self._responses:
            return '{"results":["true"]}'
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ---- 工厂 ----


def _make_unit(
    uid: str,
    scope: Scope,
    content: str,
    *,
    tier: MemoryTier = MemoryTier.WORKING,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    t_ingest: datetime | None = None,
    middle: bool = True,
) -> MemoryUnit:
    """测试用 unit 工厂——默认打 ``metadata["middle"]="true"`` 标记。

    §2.2 修复后 ``MiddleToLongJob._list_working_units`` 加了
    ``metadata.get("middle") == "true"`` 过滤——本工厂产出的候选默认
    通过此过滤。需测"非 middle 标记的 WORKING 单元不被扫到"场景时,
    传 ``middle=False``。
    """
    metadata = {"middle": "true"} if middle else {}
    unit = MemoryUnit(
        id=uid,
        scope=scope,
        tier=tier,
        lifecycle=lifecycle,
        segments=[Segment(content=content)],
        temporal=Temporal(t_ingest=t_ingest),
        metadata=metadata,
    )
    return unit


def _build_job(
    scope: Scope,
    kv,
    *,
    evolver: _RecordingEvolver | None = None,
    lifecycle: _RecordingLifecycle | None = None,
    index: _RecordingIndex | None = None,
    llm: _ScriptedLLM | None = None,
    max_fetch: int = 100,
    batch_size: int = 10,
    concurrency: int = 1,
    interval: int = 50,
) -> tuple[MiddleToLongJob, _RecordingEvolver, _RecordingLifecycle, _RecordingIndex, _ScriptedLLM]:
    evolver = evolver or _RecordingEvolver()
    lifecycle = lifecycle or _RecordingLifecycle()
    index = index or _RecordingIndex()
    llm = llm or _ScriptedLLM([])
    job = MiddleToLongJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        lifecycle=lifecycle,
        index=index,
        llm=llm,
        max_fetch=max_fetch,
        batch_size=batch_size,
        concurrency=concurrency,
        interval=interval,
    )
    return job, evolver, lifecycle, index, llm


# ---- _list_working_units ----


def test_list_working_units_filters_tier_and_lifecycle() -> None:
    """只返回 tier=WORKING + lifecycle=ACTIVE 的 unit（其他 tier / 非 ACTIVE 排除）。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    # 候选：WORKING + ACTIVE
    kv.insert(scope, memory_key("w1"), dumps(_make_unit("w1", scope, "first")))
    kv.insert(scope, memory_key("w2"), dumps(_make_unit("w2", scope, "second")))
    # 排除：EPISODIC（非 WORKING）
    kv.insert(
        scope,
        memory_key("e1"),
        dumps(_make_unit("e1", scope, "epi", tier=MemoryTier.EPISODIC)),
    )
    # 排除：ARCHIVED（非 ACTIVE）
    kv.insert(
        scope,
        memory_key("a1"),
        dumps(_make_unit("a1", scope, "archived", lifecycle=LifecycleState.ARCHIVED)),
    )
    job, *_ = _build_job(scope, kv)

    candidates = asyncio.run(job._list_working_units())  # pylint: disable=protected-access

    assert {u.id for u in candidates} == {"w1", "w2"}


def test_list_working_units_sorted_by_t_ingest_ascending() -> None:
    """按 t_ingest 升序——早的在前。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    t1 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)  # 最早
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1", t_ingest=t1)))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2", t_ingest=t2)))
    kv.insert(scope, memory_key("u3"), dumps(_make_unit("u3", scope, "c3", t_ingest=t3)))
    job, *_ = _build_job(scope, kv)

    candidates = asyncio.run(job._list_working_units())  # pylint: disable=protected-access

    assert [u.id for u in candidates] == ["u3", "u1", "u2"]


def test_list_working_units_truncates_to_max_fetch() -> None:
    """max_fetch 截断——超过 max_fetch 条按 t_ingest 升序取最近 N 条（即最早 N 条）。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    for i in range(5):
        kv.insert(
            scope,
            memory_key(f"u{i}"),
            dumps(
                _make_unit(
                    f"u{i}", scope, f"c{i}", t_ingest=datetime(2026, 1, 1, i, 0, tzinfo=timezone.utc)
                )
            ),
        )
    job, *_ = _build_job(scope, kv, max_fetch=3)

    candidates = asyncio.run(job._list_working_units())  # pylint: disable=protected-access

    assert len(candidates) == 3
    assert [u.id for u in candidates] == ["u0", "u1", "u2"]


def test_list_working_units_filters_out_non_middle_marked_units() -> None:
    """§2.2 修复回归：非 middle 标记的 WORKING+ACTIVE 单元不被扫到。

    场景：系统其他路径往 KV 写了 tier=WORKING+ACTIVE 但 metadata["middle"]
    不为 "true" 的单元（例如未来新增的缓冲场景或人工 import）。MiddleToLongJob
    只应处理 middle 路径写入的原文——不应把非 middle 单元送进 evolver 转长期。
    """
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    # middle 路径候选：应被扫到
    kv.insert(scope, memory_key("m1"), dumps(_make_unit("m1", scope, "middle-1")))
    kv.insert(scope, memory_key("m2"), dumps(_make_unit("m2", scope, "middle-2")))
    # 非 middle 标记的 WORKING+ACTIVE 单元——应被排除
    kv.insert(
        scope,
        memory_key("w1"),
        dumps(_make_unit("w1", scope, "other-working", middle=False)),
    )
    job, *_ = _build_job(scope, kv)

    candidates = asyncio.run(job._list_working_units())  # pylint: disable=protected-access

    assert {u.id for u in candidates} == {"m1", "m2"}


# ---- _check_continuity ----


def test_check_continuity_returns_llm_result() -> None:
    """LLM 返回 {"results":["true"]} → 返回 "true"。"""
    scope = Scope(user="u1")
    job, _, _, _, llm = _build_job(scope, InMemoryKVStore())
    llm._responses = ['{"results":["true"]}']  # pylint: disable=protected-access
    prev = _make_unit("p", scope, "prev")
    cur = _make_unit("c", scope, "cur")

    result = asyncio.run(job._check_continuity(prev, cur))  # pylint: disable=protected-access

    assert result == "true"
    assert len(llm.chat_calls) == 1
    # 入参必须是 ChatMessage 对象形式（不是 dict）
    msg = llm.chat_calls[0][0]
    assert isinstance(msg, ChatMessage)
    assert msg.role == "user"
    assert "{old_conversation}" not in msg.content
    assert "prev" in msg.content and "cur" in msg.content


def test_check_continuity_retries_on_bad_json_then_defaults_true() -> None:
    """LLM 3 次都返回非法 JSON → 默认 "true"（合并，复刻 mem1.0）。"""
    scope = Scope(user="u1")
    job, _, _, _, llm = _build_job(scope, InMemoryKVStore())
    llm._responses = ["not-json", "also-bad", "{broken"]  # pylint: disable=protected-access
    prev = _make_unit("p", scope, "p")
    cur = _make_unit("c", scope, "c")

    result = asyncio.run(job._check_continuity(prev, cur))  # pylint: disable=protected-access

    assert result == "true"
    assert len(llm.chat_calls) == 3  # 重试 3 次


def test_check_continuity_succeeds_on_second_attempt() -> None:
    """LLM 第一次失败、第二次成功 → 返回成功结果，不重试第三次。"""
    scope = Scope(user="u1")
    job, _, _, _, llm = _build_job(scope, InMemoryKVStore())
    llm._responses = ["not-json", '{"results":["false"]}']  # pylint: disable=protected-access
    prev = _make_unit("p", scope, "p")
    cur = _make_unit("c", scope, "c")

    result = asyncio.run(job._check_continuity(prev, cur))  # pylint: disable=protected-access

    assert result == "false"
    assert len(llm.chat_calls) == 2


# ---- _split_by_continuity ----


def test_split_by_continuity_continuous_within_batch_size() -> None:
    """连续 true 且未超 batch_size → 全部留同一批。"""
    scope = Scope(user="u1")
    job, _, _, _, _ = _build_job(scope, InMemoryKVStore(), batch_size=10)
    # 注入 LLM 响应——连续 true
    job._llm._responses = ['{"results":["true"]}'] * 5  # 6 个 unit → 5 次比较  # pylint: disable=protected-access
    units = [_make_unit(f"u{i}", scope, f"c{i}") for i in range(6)]

    batches = asyncio.run(job._split_by_continuity(units))  # pylint: disable=protected-access

    assert len(batches) == 1
    assert len(batches[0]) == 6


def test_split_by_continuity_breaks_on_false() -> None:
    """连续 false → 切批（每个 unit 独立成批）。"""
    scope = Scope(user="u1")
    job, _, _, _, _ = _build_job(scope, InMemoryKVStore())
    job._llm._responses = ['{"results":["false"]}'] * 3  # 4 个 unit → 3 次比较全 false  # pylint: disable=protected-access
    units = [_make_unit(f"u{i}", scope, f"c{i}") for i in range(4)]

    batches = asyncio.run(job._split_by_continuity(units))  # pylint: disable=protected-access

    assert len(batches) == 4
    assert [len(b) for b in batches] == [1, 1, 1, 1]


def test_split_by_continuity_respects_batch_size_upper_bound() -> None:
    """连续 true 但超 batch_size → 在 batch_size 处切批。

    语义校准：mem1.0 ``len(dialogue_batch) <= 10`` 允许 11 条（首批 1 + 后续
    10 个追加）；本实现 ``len(batches[-1]) < batch_size`` 在追加前判断，
    严格上限 = batch_size。本次 max_fetch=10 验证严格上限。
    """
    scope = Scope(user="u1")
    job, _, _, _, _ = _build_job(scope, InMemoryKVStore(), batch_size=3)
    job._llm._responses = ['{"results":["true"]}'] * 5  # 全部连续  # pylint: disable=protected-access
    units = [_make_unit(f"u{i}", scope, f"c{i}") for i in range(6)]

    batches = asyncio.run(job._split_by_continuity(units))  # pylint: disable=protected-access

    # batch_size=3：首批 [u0,u1,u2]，到 u3 时 len(batches[-1])=3 不 < 3 → 切批
    assert [len(b) for b in batches] == [3, 3]
    assert len(batches) == 2


def test_split_by_continuity_empty_returns_empty() -> None:
    """空候选 → 空批列表。"""
    scope = Scope(user="u1")
    job, *_ = _build_job(scope, InMemoryKVStore())

    assert asyncio.run(job._split_by_continuity([])) == []  # pylint: disable=protected-access


# ---- _archive_originals ----


def test_archive_originals_calls_lifecycle_transition_and_index_remove() -> None:
    """归档原文：lifecycle.transition(scope, ids, ARCHIVED) + index.remove(units)。

    真实签名校准——
    - ``transition`` 接 ``scope, unit_ids, target``（含 scope）；
    - ``index.remove`` 接 ``list[MemoryUnit]``（不是 id 列表）。
    """
    scope = Scope(user="u1")
    job, _, lifecycle, index, _ = _build_job(scope, InMemoryKVStore())
    u1 = _make_unit("u1", scope, "c1")
    u2 = _make_unit("u2", scope, "c2")

    asyncio.run(job._archive_originals([u1, u2]))  # pylint: disable=protected-access

    assert len(lifecycle.transition_calls) == 1
    s, ids, target = lifecycle.transition_calls[0]
    assert s == scope
    assert ids == ["u1", "u2"]
    assert target == LifecycleState.ARCHIVED
    assert index.removed == [u1, u2]


# ---- run 路径 ----


def test_run_returns_is_done_when_no_candidates() -> None:
    """无候选 → 返回 is_done="true"，不调 evolver / lifecycle / index。"""
    scope = Scope(user="u1")
    job, evolver, lifecycle, index, _ = _build_job(scope, InMemoryKVStore())

    info = asyncio.run(job.run())

    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["is_done"] == "true"
    assert info.detail["reason"] == "no candidates"
    assert evolver.calls == []
    assert lifecycle.transition_calls == []
    assert index.removed == []


def test_run_serial_path_calls_evolver_and_archives_processed() -> None:
    """串行路径（concurrency=1）：调 evolver + 归档转换成功的原文。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 全部连续 → 1 批
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, concurrency=1, batch_size=10
    )
    job._llm._responses = ['{"results":["true"]}']  # 2 unit → 1 次比较  # pylint: disable=protected-access

    info = asyncio.run(job.run())

    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["candidates"] == "2"
    assert info.detail["batches"] == "1"
    assert info.detail["processed"] == "2"
    assert info.detail["created_ids"] == "created-u1,created-u2"
    assert info.detail["mode"] == EvolveMode.EXTRACT.value
    assert len(evolver.calls) == 1
    units, mode = evolver.calls[0]
    assert mode == EvolveMode.EXTRACT
    assert {u.id for u in units} == {"u1", "u2"}
    # 归档原文
    assert len(lifecycle.transition_calls) == 1
    assert index.removed == units


def test_run_concurrent_path_uses_gather_and_sem() -> None:
    """并发路径（concurrency=2）：调 evolver + 归档（每批 1 unit → 2 批）。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 不连续 → 2 批
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, concurrency=2, batch_size=10
    )
    job._llm._responses = ['{"results":["false"]}']  # 1 次比较  # pylint: disable=protected-access

    info = asyncio.run(job.run())

    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["batches"] == "2"
    assert info.detail["processed"] == "2"
    assert len(evolver.calls) == 2
    assert len(lifecycle.transition_calls) == 1  # 一次性归档全部
    assert len(index.removed) == 2


def test_run_serial_failed_batch_preserves_originals() -> None:
    """串行路径失败批次：不归档原文（保留 ACTIVE+WORKING 下轮重试）。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 不连续 → 2 批；evolver 第 1 批失败
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, evolver=_RecordingEvolver(fail_on_batches=1), concurrency=1, batch_size=10
    )
    job._llm._responses = ['{"results":["false"]}']  # pylint: disable=protected-access

    info = asyncio.run(job.run())

    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["processed"] == "1"  # 只有第 2 批成功
    # 归档只有第 2 批的 unit（u2）
    assert len(lifecycle.transition_calls) == 1
    _, ids, _ = lifecycle.transition_calls[0]
    assert ids == ["u2"]
    assert len(index.removed) == 1
    assert index.removed[0].id == "u2"


def test_run_concurrent_failed_batch_preserves_originals() -> None:
    """并发路径失败批次：return_exceptions 收集，不归档失败批次原文。"""
    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 不连续 → 2 批；evolver 第 1 批失败
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, evolver=_RecordingEvolver(fail_on_batches=1), concurrency=2, batch_size=10
    )
    job._llm._responses = ['{"results":["false"]}']  # pylint: disable=protected-access

    info = asyncio.run(job.run())

    assert info.status == JobStatus.SUCCEEDED
    # 并发路径下批次完成顺序由 Semaphore 调度——不假设固定顺序，
    # 只验证 processed 总数 = 1（只有 1 批成功）
    assert info.detail["processed"] == "1"
    assert len(lifecycle.transition_calls) == 1
    assert len(index.removed) == 1


# ---- InProcessScheduler 回归测试（原崩溃路径 ①） ----
#
# 修复前链路：InProcessScheduler.submit 同步 → 内部 asyncio.run(job.run()) →
# job.run 内 MiddleToLongJob 调 asyncio.gather(_run_one(b) for b in batches) →
# 生成器在 asyncio.run 建循环前被消费 → "no current event loop" 崩溃。
# 路径 ②(AsyncTimerScheduler) 靠 _drain_queue 的 to_thread + 子 asyncio.run
# 双层跳转工作，但路径 ① 没有这层保护。
#
# 全异步改造后：submit 改 async + job.run 改 async + 内部 gather 直接 await →
# 路径 ① 不再崩溃。本组测试覆盖该回归点。


def test_in_process_scheduler_runs_middle_to_long_job_concurrent_path() -> None:
    """InProcessScheduler + MiddleToLongJob 并发路径回归。

    全异步改造前：InProcessScheduler.submit 是同步方法,内部用 asyncio.run
    跑 job.run();job.run 内 MiddleToLongJob 用 asyncio.gather 聚合并发批——
    生成器在 asyncio.run 建循环前被消费,触发 "no current event loop" 崩溃。

    改造后:submit/job.run 均 async,直接 await asyncio.gather。本测试断言
    该路径不再崩溃 + 并发分支真实跑通(evolver 收 2 批 + 原文被归档)。
    """
    from control.scheduler_impl.in_process_scheduler import InProcessScheduler
    from control.types import Channel

    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 不连续 → 2 批；并发分支(concurrency=2)
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, concurrency=2, batch_size=10
    )
    job._llm._responses = ['{"results":["false"]}']  # 1 次比较  # pylint: disable=protected-access

    scheduler = InProcessScheduler()
    job_id = asyncio.run(scheduler.submit(job, Channel.BACKGROUND))

    info = scheduler.status(job_id)
    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["batches"] == "2"
    assert info.detail["processed"] == "2"
    assert len(evolver.calls) == 2
    assert len(lifecycle.transition_calls) == 1
    assert len(index.removed) == 2


def test_in_process_scheduler_runs_middle_to_long_job_serial_path() -> None:
    """InProcessScheduler + MiddleToLongJob 串行路径回归。

    串行分支同样依赖 asyncio.to_thread(evolver.evolve)——若 InProcessScheduler
    仍用 asyncio.run 包装,Job.run 内 await asyncio.to_thread 不会崩溃
    (to_thread 在 asyncio.run 创建的循环里能正常工作),但 SUCCEEDED 状态
    流需验证。
    """
    from control.scheduler_impl.in_process_scheduler import InProcessScheduler
    from control.types import Channel

    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "c1")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "c2")))
    # 全连续 → 1 批；串行分支(concurrency=1)
    job, evolver, lifecycle, index, _ = _build_job(
        scope, kv, concurrency=1, batch_size=10
    )
    job._llm._responses = ['{"results":["true"]}']  # 1 次比较  # pylint: disable=protected-access

    scheduler = InProcessScheduler()
    job_id = asyncio.run(scheduler.submit(job, Channel.BACKGROUND))

    info = scheduler.status(job_id)
    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["batches"] == "1"
    assert info.detail["processed"] == "2"
    assert len(evolver.calls) == 1
    assert len(lifecycle.transition_calls) == 1


def test_in_process_scheduler_runs_middle_to_long_job_no_candidates() -> None:
    """InProcessScheduler + MiddleToLongJob 无候选 → is_done=true。

    验证无候选分支也能在 InProcessScheduler 链路下正常返回 SUCCEEDED +
    detail 含 is_done=true。这是原崩溃路径 ① 的边界面——空候选时 job.run
    早返回,不触发 gather,但状态流仍需正确。
    """
    from control.scheduler_impl.in_process_scheduler import InProcessScheduler
    from control.types import Channel

    scope = Scope(user="u1")
    kv = InMemoryKVStore()  # 空 KV
    job, evolver, lifecycle, index, _ = _build_job(scope, kv)

    scheduler = InProcessScheduler()
    job_id = asyncio.run(scheduler.submit(job, Channel.BACKGROUND))

    info = scheduler.status(job_id)
    assert info.status == JobStatus.SUCCEEDED
    assert info.detail["is_done"] == "true"
    assert info.detail["reason"] == "no candidates"
    assert evolver.calls == []
    assert lifecycle.transition_calls == []
    assert index.removed == []


# ---- 异步治理：to_thread 不卡事件循环 ----


def test_archive_originals_does_not_block_event_loop() -> None:
    """``_archive_originals`` 内 lifecycle.transition + index.remove 已包 to_thread——
    同步阻塞 IO 跑在独立线程,事件循环能并发跑其他协程。

    验证方式：lifecycle.transition 内 sleep 1s（模拟 Redis SDK 阻塞）,
    并发跑一个标志位协程（asyncio.sleep(0.1) 后置位）。若 _archive_originals
    包了 to_thread,标志位协程在 transition 阻塞 1s 期间能完成——总耗时 ~1s。
    若未包（旧代码同步调）,事件循环被 transition 卡 1s,标志位协程要等
    transition 跑完才能跑——总耗时 ~2s。
    """

    class _BlockingLifecycle(_RecordingLifecycle):
        """transition 内 sleep 1s 模拟同步阻塞 IO。"""

        def transition(self, scope, unit_ids, target):
            time.sleep(1.0)
            super().transition(scope, unit_ids, target)

    scope = Scope(user="u1")
    kv = InMemoryKVStore()
    job, _, lifecycle, index, _ = _build_job(
        scope, kv, lifecycle=_BlockingLifecycle()
    )
    u1 = _make_unit("u1", scope, "c1")

    marker_done = False

    async def _marker():
        """标志位协程——asyncio.sleep(0.1) 后置位。若事件循环被卡,此协程
        要等 lifecycle.transition 跑完才能跑（>1s 后才置位）。"""
        nonlocal marker_done
        await asyncio.sleep(0.1)
        marker_done = True

    async def _run():
        # 并发跑 _archive_originals + _marker
        start = time.monotonic()
        await asyncio.gather(job._archive_originals([u1]), _marker())  # pylint: disable=protected-access
        return time.monotonic() - start

    elapsed = asyncio.run(_run())

    # 标志位应被置位（_marker 协程能跑）——若 to_thread 失效,事件循环被卡,
    # _marker 跑不到置位语句
    assert marker_done, (
        "_marker 协程未在 _archive_originals 期间完成——to_thread 没生效,"
        "事件循环被同步 IO 卡住"
    )
    # 总耗时应接近 1s（lifecycle sleep）——若 to_thread 失效,_marker 等
    # transition 跑完才跑,总耗时 ~1.1s+。这里宽松断言 <1.5s。
    assert elapsed < 1.5, (
        f"总耗时 {elapsed:.2f}s > 1.5s——to_thread 没生效,事件循环被同步 IO 卡住"
    )


def test_list_working_units_does_not_block_event_loop() -> None:
    """``_list_working_units`` 内 kv.scan 已包 to_thread——同步阻塞 IO 跑在
    独立线程,事件循环能并发跑其他协程。

    验证同 _archive_originals 测试：kv.scan 内 sleep 1s,标志位协程能在
    scan 阻塞期间完成。
    """

    class _BlockingKV(InMemoryKVStore):
        """scan 内 sleep 1s 模拟同步阻塞 IO。"""

        def scan(self, scope, prefix=""):
            time.sleep(1.0)
            return super().scan(scope, prefix)

    scope = Scope(user="u1")
    kv = _BlockingKV()
    kv.insert(scope, memory_key("m1"), dumps(_make_unit("m1", scope, "c1")))
    job, *_ = _build_job(scope, kv)

    marker_done = False

    async def _marker():
        nonlocal marker_done
        await asyncio.sleep(0.1)
        marker_done = True

    async def _run():
        start = time.monotonic()
        await asyncio.gather(job._list_working_units(), _marker())  # pylint: disable=protected-access
        return time.monotonic() - start

    elapsed = asyncio.run(_run())

    assert marker_done, (
        "_marker 协程未在 _list_working_units 期间完成——to_thread 没生效"
    )
    assert elapsed < 1.5, (
        f"总耗时 {elapsed:.2f}s > 1.5s——to_thread 没生效,事件循环被同步 IO 卡住"
    )
