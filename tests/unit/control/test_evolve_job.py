"""EvolveJob——把现有 InProcessScheduler._execute_task 逻辑外提为 Job 类。

mode 由构造参数注入，支持 EXTRACT/ASSOCIATE/CONSOLIDATE/FORGET 任意值，
忠实于原 ``submit(scope, mode, channel)`` 的 mode 语义。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.construction import EvolveMode, Evolver, EvolveResult
from jiuwen_memory.construction.base import OperatorType
from jiuwen_memory.control.jobs_impl.evolve_job import EvolveJob
from jiuwen_memory.control.types import JobStatus
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

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
        id=uid, scope=scope, segments=[Segment(content=content)], system_metadata={"middle": "true"}
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

    job = EvolveJob(
        scope=scope,
        kv=kv,
        evolver=evolver,
        mode=EvolveMode.CONSOLIDATE,
    )
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
    kv.insert(
        scope, memory_key("mid-2"), dumps(_make_middle_unit("mid-2", scope, "another middle"))
    )
    evolver = RecordingEvolver()

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    info = asyncio.run(job.run())

    units, _ = evolver.calls[0]
    assert {u.id for u in units} == {"long-1", "long-2"}
    assert info.status == JobStatus.SUCCEEDED


def test_job_default_interval_is_zero_meaning_one_shot() -> None:
    """默认 interval=0 表示一次性任务（语义校验，非 run 行为）。"""
    scope = Scope(org="acme", user="u1")
    job = EvolveJob(
        scope=scope,
        kv=InMemoryKVStore(),
        evolver=RecordingEvolver(),
    )
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


# ====================================================================== #
# evolver_for_unit 分组分派（mem2.0 §2.5.1 改动 1）
# ====================================================================== #


class TaggedEvolver(Evolver):
    """按自身 tag 产出结果标识的 Evolver 替身——验证结果合并的来源可辨。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[list[str]] = []

    def operator_type(self) -> OperatorType:
        return OperatorType.EVOLVER

    def health(self) -> None:
        return None

    def evolve(self, units: list[MemoryUnit], mode: EvolveMode) -> EvolveResult:
        self.calls.append([unit.id for unit in units])
        return EvolveResult(
            created_ids=[f"created-{self.tag}"],
            updated_ids=[f"updated-{self.tag}"],
            superseded_ids=[f"superseded-{self.tag}"],
            forgotten_ids=[f"forgotten-{self.tag}"],
            created_units=(
                [_make_unit(f"new-{self.tag}", units[0].scope, "")]
                if units
                else []
            ),
            duplicate_ids=[f"dup-{self.tag}"],
        )


def _make_typed_unit(uid: str, scope: Scope, memory_type: str) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=scope,
        segments=[Segment(content=f"content of {uid}")],
        system_metadata={"memory_type": memory_type},
    )


def test_evolver_for_unit_dispatches_groups_and_merges_results() -> None:
    """逐条选取 evolver 按实例分组分别演进，六字段结果合并回显。

    Dreaming 读入的条目按 memory_type 分发（mem2.0 §2.5.1 改动 1）：经验
    条目交经验 profile 的 evolver，其余交全局 evolver。
    """
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    for uid, memory_type in (
        ("exp-1", "experience"),
        ("plain-1", "semantic"),
        ("exp-2", "experience"),
        ("plain-2", "semantic"),
    ):
        kv.insert(scope, memory_key(uid), dumps(_make_typed_unit(uid, scope, memory_type)))
    global_evolver = TaggedEvolver("global")
    experience_evolver = TaggedEvolver("experience")

    def evolver_for_unit(unit: MemoryUnit) -> Evolver | None:
        if unit.system_metadata.get("memory_type") == "experience":
            return experience_evolver
        return None  # 回落全局

    job = EvolveJob(
        scope=scope,
        kv=kv,
        evolver=global_evolver,
        evolver_for_unit=evolver_for_unit,
    )
    info = asyncio.run(job.run())

    assert sorted(experience_evolver.calls[0]) == ["exp-1", "exp-2"], (
        "经验条目整组交经验 evolver（组内顺序随 KV 枚举）"
    )
    assert sorted(global_evolver.calls[0]) == ["plain-1", "plain-2"], (
        "其余条目整组交全局 evolver"
    )
    assert info.status == JobStatus.SUCCEEDED
    # 两组结果合并回显（组间顺序随首条枚举序，用集合比较；created_units
    # 是对象列表，进 detail 的是其余 id 字段）
    assert set(info.detail["created_ids"].split(",")) == {
        "created-experience", "created-global"
    }
    assert set(info.detail["updated_ids"].split(",")) == {
        "updated-experience", "updated-global"
    }
    assert set(info.detail["superseded_ids"].split(",")) == {
        "superseded-experience", "superseded-global"
    }
    assert set(info.detail["forgotten_ids"].split(",")) == {
        "forgotten-experience", "forgotten-global"
    }


def test_evolver_for_unit_none_keeps_whole_batch_on_global_evolver() -> None:
    """未配 evolver_for_unit 时整批交全局 evolver——旧调用路径行为不变。"""
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    kv.insert(scope, memory_key("u1"), dumps(_make_unit("u1", scope, "one")))
    kv.insert(scope, memory_key("u2"), dumps(_make_unit("u2", scope, "two")))
    evolver = TaggedEvolver("global")

    job = EvolveJob(scope=scope, kv=kv, evolver=evolver)
    asyncio.run(job.run())

    assert len(evolver.calls) == 1 and sorted(evolver.calls[0]) == ["u1", "u2"], (
        "无选择器时整批一次交全局（现行为，组内顺序随 KV 枚举）"
    )


def test_evolver_for_unit_with_empty_bucket_still_calls_global_evolver() -> None:
    """空桶仍调全局 evolver（既有契约）——分组路径不破坏单桶空跑语义。"""
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()  # 空 scope
    evolver = TaggedEvolver("global")
    selector_calls: list[str] = []

    def evolver_for_unit(unit: MemoryUnit) -> Evolver | None:
        selector_calls.append(unit.id)
        return None

    job = EvolveJob(
        scope=scope, kv=kv, evolver=evolver, evolver_for_unit=evolver_for_unit
    )
    info = asyncio.run(job.run())

    assert selector_calls == [], "空桶不触发逐条选取"
    assert evolver.calls == [[]], "空桶仍调全局 evolver 一次（空列表）"
    assert info.status == JobStatus.SUCCEEDED
