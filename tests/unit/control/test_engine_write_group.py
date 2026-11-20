# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Engine ``write_group`` 的合并抽取、分段与逐条归集（F10 决策 4、5、6）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwen_memory.common.type_def import MemoryUnit, Modality, Scope, Segment, Temporal
from jiuwen_memory.construction.evolver import EvolveResult, GroupedExtractResult
from jiuwen_memory.construction.index_builder import UnitCommitState, UnitCommitStatus
from jiuwen_memory.control.engine_impl.in_memory_engine import InMemoryEngine
from jiuwen_memory.control.types import BatchWriteItem
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

# 用例直接驱动内核私有入口（API/引擎/校验器），按仓库既有做法整文件豁免。
# pylint: disable=protected-access

pytestmark = pytest.mark.unit

_SCOPE = Scope(org="acme", space="", user="u1")


def _unit(unit_id: str, content: str, provenance: list[str] | None = None) -> MemoryUnit:
    return MemoryUnit(
        id=unit_id,
        scope=_SCOPE,
        segments=[Segment(content=content, source=Modality.TEXT)],
        temporal=Temporal(),
        provenance=list(provenance or []),
    )


class _Ingestor:
    def __init__(self) -> None:
        self.units: list[MemoryUnit] = []

    def ingest(self, payloads):
        produced = []
        for payload in payloads:
            unit = _unit(f"src-{len(self.units) + 1}", payload.data.decode())
            self.units.append(unit)
            produced.append(unit)
        return produced


class _Evolver:
    def __init__(self, *, grouped: bool = True, merge_result=None) -> None:
        self.grouped = grouped
        self.merge_result = merge_result
        self.grouped_calls: list[list[str]] = []
        self.single_calls: list[list[str]] = []

    def supports_grouped_extract(self) -> bool:
        return self.grouped

    def evolve(self, units, mode=None):
        self.single_calls.append([unit.id for unit in units])
        derived = _unit(f"d-{units[0].id}", "派生", provenance=[units[0].id])
        return EvolveResult(created_ids=[derived.id], created_units=[derived])

    def evolve_grouped(self, units):
        self.grouped_calls.append([unit.id for unit in units])
        if self.merge_result is not None:
            return self.merge_result
        derived = _unit("d-merged", "合并派生", provenance=[units[0].id])
        return GroupedExtractResult(
            result=EvolveResult(created_ids=[derived.id], created_units=[derived]),
            sources={derived.id: [units[0].id]},
        )


class _Builder:
    def __init__(self, *, grouped: bool = True, statuses=None) -> None:
        self.grouped = grouped
        self.statuses = statuses
        self.build_calls: list[list[str]] = []
        self.grouped_calls: list[list[str]] = []

    def supports_grouped_build(self) -> bool:
        return self.grouped

    def build(self, units):
        self.build_calls.append([unit.id for unit in units])

    def build_grouped(self, units):
        self.grouped_calls.append([unit.id for unit in units])
        if self.statuses is not None:
            return self.statuses
        return [
            UnitCommitStatus(unit_id=unit.id, state=UnitCommitState.COMMITTED)
            for unit in units
        ]


def _engine(evolver=None, builder=None, ingestor=None) -> InMemoryEngine:
    return InMemoryEngine(
        ingestor=ingestor or _Ingestor(),
        index_builder=builder or _Builder(),
        retriever=None,
        kv=InMemoryKVStore(),
        scheduler=None,
        evolver=evolver or _Evolver(),
        lifecycle=None,
    )


def _items(count: int, *, infer: bool = True) -> list[BatchWriteItem]:
    metadata = {"infer": "true"} if infer else {}
    return [
        BatchWriteItem(content=f"记忆{i}", scope=_SCOPE, system_metadata=dict(metadata))
        for i in range(count)
    ]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_two_items_one_group_merge_calls_extract_once() -> None:
    evolver = _Evolver()
    engine = _engine(evolver=evolver)

    outcomes = _run(engine.write_group(_items(2), merge_extract=True))

    assert len(evolver.grouped_calls) == 1, "同组两条必须只调一次合并抽取"
    assert len(evolver.grouped_calls[0]) == 2, "合并抽取应收到两条原文的 unit"
    assert evolver.single_calls == [], "合并成功时不得退回逐条抽取"
    assert [outcome.index for outcome in outcomes] == [0, 1], "结果必须与输入顺序对齐"


def test_merge_extract_false_calls_per_item() -> None:
    evolver = _Evolver()
    engine = _engine(evolver=evolver)

    _run(engine.write_group(_items(2), merge_extract=False))

    assert evolver.grouped_calls == [], "关闭合并时不得调用合并抽取"
    assert len(evolver.single_calls) == 2, "关闭合并时逐条抽取"


def test_attribution_covered_and_uncovered() -> None:
    """有来源的派生记忆归对应条目；无来源的整批共享并留下信号。"""

    def merge(units):
        sourced = _unit("d-sourced", "有来源", provenance=[units[1].id])
        orphan = _unit("d-orphan", "无来源")
        return GroupedExtractResult(
            result=EvolveResult(
                created_ids=["d-sourced", "d-orphan"], created_units=[sourced, orphan]
            ),
            sources={"d-sourced": [units[1].id]},
        )

    evolver = _Evolver(merge_result=None)
    evolver.evolve_grouped = merge  # type: ignore[method-assign]
    engine = _engine(evolver=evolver)

    outcomes = _run(engine.write_group(_items(2), merge_extract=True))

    first = {unit.id for unit in outcomes[0].units}
    second = {unit.id for unit in outcomes[1].units}
    assert "d-sourced" in second and "d-sourced" not in first, "有来源的只归来源条目"
    assert "d-orphan" in first and "d-orphan" in second, "无来源的整批共享"

    assert engine.write_signals.counters()["shared_attribution"] == 1, (
        "无来源派生记忆必须恰好留下一次可观测信号"
    )


def _orchestrator():
    from jiuwen_memory.common.lock.lock_impl.in_memory_lock import InMemoryLockProvider
    from jiuwen_memory.control.write_orchestrator_impl.grouped import GroupedWriteOrchestrator

    return GroupedWriteOrchestrator(lock=InMemoryLockProvider(wait_timeout_ms=5_000))


def test_extract_unsupported_degrades_within_segment() -> None:
    from jiuwen_memory.control.write_orchestrator import WriteTask

    evolver = _Evolver(grouped=False)
    engine = _engine(evolver=evolver)
    orchestrator = _orchestrator()

    _run(orchestrator.write(engine, WriteTask(items=_items(3), merge_extract=True)))

    assert evolver.grouped_calls == [], "未声明 grouped_extract 时只能逐条抽取"
    assert len(evolver.single_calls) == 3, "逐条抽取发生在段内，不改变分组编排"
    counters = orchestrator.degrade_counters()
    assert counters == {}, "能力缺失只在段内降级，不应把整批推到逐条编排"
    assert engine.write_signals.counters().get("extract_unsupported") == 1, (
        "Evolver 不支持合并抽取时应记一次可观测信号"
    )


def test_build_unsupported_degrades_within_segment() -> None:
    from jiuwen_memory.control.write_orchestrator import WriteTask

    builder = _Builder(grouped=False)
    engine = _engine(builder=builder)
    orchestrator = _orchestrator()

    _run(orchestrator.write(engine, WriteTask(items=_items(3, infer=False))))

    assert orchestrator.degrade_counters() == {}, "构建器不支持批量 build 不拦分组编排"
    assert engine.write_signals.counters().get("build_unsupported") == 1, (
        "直写段逐条 build 时应记一次可观测信号"
    )
    assert builder.grouped_calls == [], "未声明 grouped_build 时不得调用批量接口"
    assert len(builder.build_calls) == 3, "直写段内逐条落盘，组间并发保留"


def test_document_builder_still_merges_infer_batch() -> None:
    """文档栈（builder 不支持批量 build）也必须享受合并抽取与组间并发。"""
    from jiuwen_memory.control.write_orchestrator import WriteTask

    builder = _Builder(grouped=False)
    evolver = _Evolver(grouped=True)
    engine = _engine(evolver=evolver, builder=builder)
    orchestrator = _orchestrator()

    _run(orchestrator.write(engine, WriteTask(items=_items(3), merge_extract=True)))

    assert len(evolver.grouped_calls) == 1, "3 条 infer 应合并为一次抽取"
    assert len(evolver.grouped_calls[0]) == 3, "合并抽取应收到全部 3 条原文"
    assert evolver.single_calls == [], "合并成功后不得逐条重抽"
    assert builder.build_calls == [] and builder.grouped_calls == [], (
        "infer 段不经过引擎构建器"
    )


def test_no_degrade_signal_for_single_item_or_fail_fast() -> None:
    from jiuwen_memory.control.write_orchestrator import WriteTask

    engine = _engine(evolver=_Evolver(grouped=False))
    orchestrator = _orchestrator()

    _run(orchestrator.write(engine, WriteTask(items=_items(1))))
    _run(orchestrator.write(engine, WriteTask(items=_items(3), continue_on_error=False)))

    assert orchestrator.degrade_counters() == {}, (
        "单条写入与 continue_on_error=False 本就逐条执行，不算降级"
    )


def test_merge_failure_with_unknown_commit_records_no_replay() -> None:
    class _BoomEvolver(_Evolver):
        def evolve_grouped(self, units):
            raise RuntimeError("merge boom")

    evolver = _BoomEvolver()
    engine = _engine(evolver=evolver)

    outcomes = _run(engine.write_group(_items(2), merge_extract=True))

    assert engine.write_signals.counters()["unknown_no_replay"] == 1, (
        "合并失败且提交状态未知时不得静默重放，必须留下信号"
    )
    assert outcomes[0].error_type == "InternalError"
    assert evolver.single_calls == [], "提交状态未知时不得逐条重放"


def test_one_item_two_units_partial() -> None:
    class _TwoUnits(_Ingestor):
        def ingest(self, payloads):
            return [_unit("a", "第一条"), _unit("b", "第二条")]

    builder = _Builder(
        statuses=[
            UnitCommitStatus(unit_id="a", state=UnitCommitState.COMMITTED),
            UnitCommitStatus(
                unit_id="b", state=UnitCommitState.UNKNOWN, error=RuntimeError("boom")
            ),
        ]
    )
    engine = _engine(builder=builder, ingestor=_TwoUnits())

    outcomes = _run(engine.write_group(_items(1, infer=False), merge_extract=False))

    outcome = outcomes[0]
    assert [unit.id for unit in outcome.units] == ["a"], "已提交的 unit 仍返回"
    assert outcome.error, "未提交的 unit 必须带错误"
    assert outcome.error_type == "WriteIncompleteError", "落盘结论未知时按不完全写入报告"


def test_build_capability_does_not_block_grouped_write() -> None:
    builder = _Builder(grouped=False)
    engine = _engine(builder=builder)

    assert engine.supports_grouped_write(_items(2)) is True, (
        "构建器不支持批量 build 只让直写段逐条落盘，不应把整批判为不支持分组"
    )
    _run(engine.write_group(_items(2, infer=False), merge_extract=False))
    assert len(builder.build_calls) == 2, "段内逐条 build"


def test_write_group_order_preserved() -> None:
    engine = _engine()

    outcomes = _run(engine.write_group(_items(3), merge_extract=True))

    assert [outcome.index for outcome in outcomes] == [0, 1, 2]
    assert [outcome.item.content for outcome in outcomes] == ["记忆0", "记忆1", "记忆2"]


def test_single_write_still_uses_grouped_builder() -> None:
    builder = _Builder()
    engine = _engine(builder=builder)

    units = _run(
        engine.write("直写一条", _SCOPE, system_metadata={})
    )

    assert [unit.id for unit in units] == ["src-1"], "单条 write 走同一套 prepare/execute"
    assert builder.build_calls == [] and len(builder.grouped_calls) == 1, (
        "支持批量建索引的构建器统一走 build_grouped"
    )
    assert isinstance(engine, SimpleNamespace | InMemoryEngine)


def test_builder_missing_status_treated_as_unknown() -> None:
    """插件漏报某条 unit 时不能当成已提交。"""

    class _PartialBuilder(_Builder):
        def build_grouped(self, units):
            return [UnitCommitStatus(unit_id=units[0].id, state=UnitCommitState.COMMITTED)]

    class _TwoUnits(_Ingestor):
        def ingest(self, payloads):
            return [_unit("a", "第一条"), _unit("b", "第二条")]

    engine = _engine(builder=_PartialBuilder(), ingestor=_TwoUnits())

    outcomes = _run(engine.write_group(_items(1, infer=False), merge_extract=False))

    outcome = outcomes[0]
    assert [unit.id for unit in outcome.units] == ["a"], "有回执且已提交的仍返回"
    assert outcome.error_type == "WriteIncompleteError", "缺失回执按无法确认处理"
    assert "b" in outcome.error, "错误信息要指出受影响的 unit"


def test_builder_failed_without_error_reports_failure() -> None:
    """FAILED 且 error 为空也必须报错，不能返回「成功但没 unit」。"""

    class _SilentFailure(_Builder):
        def build_grouped(self, units):
            return [
                UnitCommitStatus(unit_id=unit.id, state=UnitCommitState.FAILED)
                for unit in units
            ]

    engine = _engine(builder=_SilentFailure())

    outcome = _run(engine.write_group(_items(1, infer=False), merge_extract=False))[0]

    assert outcome.units == []
    assert outcome.error, "FAILED 无错误文本时也要给出可诊断的错误"
    assert outcome.error_type != ""


def test_prepare_failure_isolated_per_item() -> None:
    """一条 ingest 抛错不能带走整批的其余条目。"""

    class _FlakyIngestor(_Ingestor):
        def ingest(self, payloads):
            if payloads[0].data.decode() == "记忆1":
                raise RuntimeError("ingest failed")
            return super().ingest(payloads)

    engine = _engine(ingestor=_FlakyIngestor())

    outcomes = _run(engine.write_group(_items(3, infer=False), merge_extract=False))

    assert outcomes[1].error, "失败条目标失败"
    assert outcomes[0].units and outcomes[2].units, "前后两条必须照常写入"
    assert [outcome.index for outcome in outcomes] == [0, 1, 2], "结果仍与输入对齐"


def test_per_item_build_failure_isolated() -> None:
    """只支持逐项 build 的插件里，中间一条失败不连累后续条目。"""

    class _PerItemBuilder(_Builder):
        def __init__(self) -> None:
            super().__init__(grouped=False)

        def build(self, units):
            self.build_calls.append([unit.id for unit in units])
            if units[0].content == "记忆1":
                raise RuntimeError("build failed")

    engine = _engine(builder=_PerItemBuilder())

    outcomes = _run(engine.write_group(_items(3, infer=False), merge_extract=False))

    assert outcomes[0].units and outcomes[2].units, "失败条目之外的两条必须写入"
    assert outcomes[1].error and outcomes[1].error_type == "InternalError"


def test_middle_item_failure_is_isolated() -> None:
    """middle 段里 A 成功、B 失败时，A 不能被改报失败，C 必须继续执行。"""

    builder = _Builder()
    engine = _engine(builder=builder)

    async def middle(entry):
        if entry.item.content == "记忆1":
            raise RuntimeError("middle boom")
        return list(entry.units)

    engine._execute_middle_item = middle  # type: ignore[method-assign]

    items = [
        BatchWriteItem(
            content=f"记忆{i}", scope=_SCOPE, system_metadata={"infer": "true", "middle": "true"}
        )
        for i in range(3)
    ]
    outcomes = _run(engine.write_group(items, merge_extract=False))

    assert outcomes[0].units and outcomes[2].units, "失败条目之外的必须保留产物"
    assert not outcomes[0].error, "已成功的条目不得被改报失败"
    assert outcomes[1].error and outcomes[1].error_type == "InternalError"
    assert builder.grouped_calls == [] and builder.build_calls == [], "middle 不建索引"
