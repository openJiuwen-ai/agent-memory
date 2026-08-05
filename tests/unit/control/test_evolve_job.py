"""EvolveJob——把现有 InProcessScheduler._execute_task 逻辑外提为 Job 类。

mode 由构造参数注入，支持 EXTRACT/ASSOCIATE/CONSOLIDATE/FORGET 任意值，
忠实于原 ``submit(scope, mode, channel)`` 的 mode 语义。
"""

from __future__ import annotations

import asyncio

import pytest

from common.type_def import MemoryUnit, Segment, Scope, memory_key
from common.type_def.memory_codec import dumps
from construction import EvolveMode, EvolveResult, Evolver
from construction.base import OperatorType
from control.jobs_impl.evolve_job import EvolveJob
from control.types import JobStatus
from storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


class RecordingEvolver(Evolver):
    """记录调用入参的 Evolver 替身（不依赖真实构建链）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[MemoryUnit], EvolveMode]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append((units, mode))
        return EvolveResult(
            created_ids=["created-1"],
            updated_ids=["updated-1"],
            superseded_ids=["old-1"],
            forgotten_ids=["forgotten-1"],
        )


def _make_unit(uid: str, scope: Scope, content: str) -> MemoryUnit:
    return MemoryUnit(
        id=uid, scope=scope, segments=[Segment(content=content)]
    )


def _make_middle_unit(uid: str, scope: Scope, content: str) -> MemoryUnit:
    """带 ``metadata["middle"]="true"`` 标记的中期记忆单元。"""
    return MemoryUnit(
        id=uid, scope=scope, segments=[Segment(content=content)], metadata={"middle": "true"}
    )


def test_run_loads_all_scope_units_and_calls_evolver_with_default_extract_mode() -> None:
    """list scope 全部 MemoryUnit + 调 evolver.evolve(units, EXTRACT)。

    默认 mode=EXTRACT（与原 InProcessScheduler.submit 的常用入参一致）。
    """
    scope = Scope(org="acme", user="u1")
    other_scope = Scope(org="acme", user="u2")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("unit-1"), dumps(_make_unit("unit-1", scope, "one")))
    kv.insert(scope, memory_key("unit-2"), dumps(_make_unit("unit-2", scope, "two")))
    # 其他 scope 的记录不应被 EvolveJob 拉到
    kv.insert(
        other_scope,
        memory_key("other-unit"),
        dumps(_make_unit("other-unit", other_scope, "other")),
    )
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    info = asyncio.run(job.run())

    assert len(evolver.calls) == 1
    units, mode = evolver.calls[0]
    assert mode == EvolveMode.EXTRACT
    assert {u.id for u in units} == {"unit-1", "unit-2"}
    assert info.status == JobStatus.SUCCEEDED
    assert info.scope == scope
    assert info.detail["created_ids"] == "created-1"
    assert info.detail["updated_ids"] == "updated-1"
    assert info.detail["superseded_ids"] == "old-1"
    assert info.detail["forgotten_ids"] == "forgotten-1"
    assert info.detail["mode"] == EvolveMode.EXTRACT.value


def test_run_calls_evolver_with_explicit_mode_from_constructor() -> None:
    """mode 由构造参数注入——传 CONSOLIDATE 时 evolver 收到 CONSOLIDATE 而非 EXTRACT。

    忠实于原 submit(scope, mode, channel) 的 mode 语义——mode 不应硬编码。
    """
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("unit-1"), dumps(_make_unit("unit-1", scope, "one")))
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver, mode=EvolveMode.CONSOLIDATE)
    info = asyncio.run(job.run())

    _, mode = evolver.calls[0]
    assert mode == EvolveMode.CONSOLIDATE
    assert info.detail["mode"] == EvolveMode.CONSOLIDATE.value


def test_run_skips_non_memory_unit_records_via_loads_filter() -> None:
    """loads 对非 dict 的合法 JSON 记录返回 None，自然过滤。

    loads 文档承诺"碰到非 dict 时返回 None"——指合法 JSON 但非 dict 类型
    （如 list/number），不包括非合法 JSON 字节（那种仍会抛 JSONDecodeError，
    与原 InProcessScheduler._execute_task 行为一致）。
    """
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("unit-1"), dumps(_make_unit("unit-1", scope, "one")))
    # 一条非 MemoryUnit 记录（合法 JSON list）——不应进入 evolver
    kv.insert(scope, "/memory/index-only", b"[1, 2, 3]")
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    info = asyncio.run(job.run())

    units, _ = evolver.calls[0]
    assert {u.id for u in units} == {"unit-1"}
    assert info.status == JobStatus.SUCCEEDED


def test_run_with_empty_scope_still_calls_evolver_with_empty_list() -> None:
    """空 scope 下仍调 evolver（空 units 列表），不抛错——与 InProcessScheduler 行为一致。"""
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    info = asyncio.run(job.run())

    units, _ = evolver.calls[0]
    assert units == []
    assert info.status == JobStatus.SUCCEEDED


def test_run_excludes_middle_marked_units_from_evolver_input() -> None:
    """§2.2 修复回归：middle=true 标记的中期记忆不应送进 evolver。

    场景：KV 中混有 middle 路径写入的原文（``metadata["middle"]="true"``）
    和普通长期记忆。EvolveJob 作为通用演进入口，应排除中期记忆——
    这些由 :class:`MiddleToLongJob` 专门处理（转换+归档链路）。
    若 EvolveJob 也把它们送进 evolver，同一原文会被两次处理，导致
    重复 created_ids + 与 MiddleToLongJob 的归档冲突。
    """
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    # 普通长期记忆——应进入 evolver
    kv.insert(scope, memory_key("long-1"), dumps(_make_unit("long-1", scope, "long term")))
    kv.insert(scope, memory_key("long-2"), dumps(_make_unit("long-2", scope, "another long")))
    # 中期记忆原文——应被排除
    kv.insert(scope, memory_key("mid-1"), dumps(_make_middle_unit("mid-1", scope, "middle raw")))
    kv.insert(scope, memory_key("mid-2"), dumps(_make_middle_unit("mid-2", scope, "another middle")))
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    info = asyncio.run(job.run())

    units, _ = evolver.calls[0]
    assert {u.id for u in units} == {"long-1", "long-2"}
    assert info.status == JobStatus.SUCCEEDED


def test_job_default_interval_is_zero_meaning_one_shot() -> None:
    """默认 interval=0 表示一次性任务（语义校验，非 run 行为）。"""
    scope = Scope(org="acme", user="u1")
    job = EvolveJob(scope=scope, kv=InMemoryKVStore(), evolver=RecordingEvolver())
    assert job.interval == 0


def test_job_accepts_explicit_interval_for_timer_declaration() -> None:
    """显式传 interval>0 时作为定时任务声明（语义校验）。"""
    scope = Scope(org="acme", user="u1")
    job = EvolveJob(
        scope=scope,
        kv=InMemoryKVStore(),
        evolver=RecordingEvolver(),
        interval=50,
    )
    assert job.interval == 50
