# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""三个 Engine 共用的写入编排：``prepare`` 与分组 ``execute``（F10 决策 5）。

单条 ``write`` 与批量 ``write_group`` 共用同一套执行代码，避免长期出现两份实现：

- ``_prepare(item)``：引擎各自实现。构造 ``RawPayload``、跑 Ingestor、做引擎特有的
  metadata 规整与标记，并选出本次的组件（evolver / index_builder / classifier）。
- ``execute_group(prepared, *, merge_extract)``：本模块的公共实现。按「路径 + 组件」
  切段，分别执行合并抽取 / 直写 / procedural / middle，并把结果分回各条目。

分组与并发由 ``WriteOrchestrator`` 负责；本模块只处理**同一调用内**的分段与失败隔离。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jiuwen_memory.common.errors import AgentMemoryError, WriteIncompleteError
from jiuwen_memory.common.ingest_cancellation import IngestCancelled
from jiuwen_memory.common.log import get_logger, metadata_for_log
from jiuwen_memory.common.trace import TraceLevel, trace_event, traced
from jiuwen_memory.common.type_def import MemoryUnit, Scope
from jiuwen_memory.construction import (
    EvolveMode,
    Evolver,
    EvolveResult,
    ExtractNotCommittedError,
    IndexBuilder,
    UnitCommitState,
)
from jiuwen_memory.control.ingest_job import (
    begin_ingest_write,
    record_ingest_ids,
    record_ingest_units,
)
from jiuwen_memory.control.pipeline import PipelineBinding
from jiuwen_memory.control.types import BatchWriteItem, BatchWriteOutcome
from jiuwen_memory.control.write_orchestrator_impl.signals import (
    DegradeEvent,
    WriteSignals,
)

logger = get_logger(__name__)


def supports_grouped_build(builder) -> bool:
    """查询批量建索引能力；未提供该方法的替身/旧实现按不支持处理。"""
    fn = getattr(builder, "supports_grouped_build", None)
    return bool(fn()) if callable(fn) else False


def supports_grouped_extract(evolver) -> bool:
    """查询合并抽取能力；未提供该方法的替身/旧实现按不支持处理。"""
    fn = getattr(evolver, "supports_grouped_extract", None)
    return bool(fn()) if callable(fn) else False


def executor_from_config(config) -> object | None:
    """引擎装配期从 ``engine.params.executor`` 取执行器；未声明时退回 to_thread。

    执行器是具名共享实例（默认 ``executor.default``），三个引擎与 API 层拿同一份。
    """
    from jiuwen_memory.control.executor import BlockingExecutorProducer

    if config.params.get("executor") is None:
        return None
    return BlockingExecutorProducer.dep(config, "executor")


_LEGACY_PATH = {
    "procedural": "procedural",
    "middle": "middle",
    "infer": "infer",
    "direct": "raw",
}


def legacy_path_name(path: WritePath) -> str:
    """写入路径的对内名称；trace 沿用历史字符串（direct → raw），避免破坏已有观测。"""
    return _LEGACY_PATH.get(path.value, path.value)


class WritePath(str, Enum):
    """本条写入走哪条路径；分段与合并判定都以它为准。"""

    PROCEDURAL = "procedural"
    MIDDLE = "middle"
    INFER = "infer"
    DIRECT = "direct"


@dataclass
class PreparedWrite:
    """``_prepare`` 的产物：已 ingest 的 unit 与本次选出的组件。"""

    item: BatchWriteItem
    units: list[MemoryUnit]
    path: WritePath
    evolver: Evolver | None = None
    index_builder: IndexBuilder | None = None
    classifier: Any = None
    binding: PipelineBinding | None = None
    middle_interval: int | None = None
    pipeline_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class GroupedWriteMixin:
    """给三个 Engine 提供 ``_execute_group`` / ``write_group`` 的公共实现。

    引擎需提供 ``self._executor``（``BlockingExecutor | None``）；为 ``None`` 时退回
    ``asyncio.to_thread``，保证旧装配与单测不依赖执行器也能工作。
    """

    @property
    def write_signals(self) -> WriteSignals:
        """本引擎实例的降级计数（装配期可与编排器共享同一实例）。

        ``_signals`` 由各引擎在 ``__init__`` 中初始化，本属性只读——避免在
        ``__init__`` 之外引入实例属性。
        """
        return self._signals

    # -- 执行器适配 ---------------------------------------------------------- #

    async def _run_blocking(self, lane, fn, *args, **kwargs):
        """把同步调用交给执行器对应通道；未装配执行器时退回默认线程池。"""
        if getattr(self, "_executor", None) is None:
            return await asyncio.to_thread(fn, *args, **kwargs)
        return await self._executor.run(lane, fn, *args, **kwargs)

    # -- 分段 ---------------------------------------------------------------- #

    @staticmethod
    def _segment_key(prepared: PreparedWrite) -> tuple:
        return (
            prepared.path,
            id(prepared.binding),
            id(prepared.evolver),
            id(prepared.index_builder),
            id(prepared.classifier),
        )

    @classmethod
    def _segments(cls, prepared: list[PreparedWrite]) -> list[list[PreparedWrite]]:
        segments: list[list[PreparedWrite]] = []
        for entry in prepared:
            if segments and cls._segment_key(segments[-1][0]) == cls._segment_key(entry):
                segments[-1].append(entry)
            else:
                segments.append([entry])
        return segments

    # -- 入口 ---------------------------------------------------------------- #

    @traced("write", level=TraceLevel.SIMPLE)
    async def write_group(
        self, items: list[BatchWriteItem], *, merge_extract: bool,
    ) -> list[BatchWriteOutcome]:
        """批量写入：逐条 prepare，再按路径分段执行。结果与 ``items`` 逐条对齐。

        prepare 的异常按条隔离：一条 ingest 失败不能带走整批（``continue_on_error=True``
        时后续条目必须继续写）。prepare 在工作线程执行——Ingestor 可以是同步阻塞实现。
        """
        outcomes = [
            BatchWriteOutcome(index=index, item=item) for index, item in enumerate(items)
        ]
        prepared: list[PreparedWrite] = []
        positions: list[int] = []
        for index, item in enumerate(items):
            try:
                prepared.append(await self._prepare_async(item))
            except Exception as exc:  # noqa: BLE001 —— 单条失败不扩散到整批
                _raise_if_cancelled(exc)
                _set_failure(outcomes[index], exc)
                continue
            positions.append(index)
        if not prepared:
            return outcomes
        group_outcomes = await self._execute_group(prepared, merge_extract=merge_extract)
        for position, outcome in zip(positions, group_outcomes):
            outcome.index = position
            outcomes[position] = outcome
        return outcomes

    def _supports_grouped_write(self, items: list[BatchWriteItem]) -> bool:
        return self._grouped_write_blocker(items) is None

    def _grouped_write_blocker(self, items: list[BatchWriteItem]) -> str | None:
        """判断整批能否走分组编排：只拦「组件解析不出」这种引擎级降级。

        构建器不支持批量建索引，只影响直写段的落盘方式——段内逐条 build，组间并发照旧；
        Evolver 不支持合并抽取，只影响 infer 段的抽取方式——段内逐条抽取。两者都由
        ``_execute_group`` 按段降级并记信号，不应把整批推到逐条执行（那会让文档栈等
        只做逐条 build 的装配失去组间并发与合并抽取）。
        """
        if not items:
            return DegradeEvent.ENGINE_DEGRADED.value
        for item in items:
            binding = self._binding_for_item(item)
            if getattr(self, "_pipeline", None) is not None and binding is None:
                return DegradeEvent.ENGINE_DEGRADED.value
        return None

    def _binding_for_item(self, item: BatchWriteItem) -> PipelineBinding | None:
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is None:
            return None
        explicit_fn = getattr(pipeline, "explicit_write_binding", None)
        explicit = explicit_fn(item.system_metadata or {}) if callable(explicit_fn) else None
        if explicit is not None:
            return explicit
        default = getattr(pipeline, "default_write_binding", None)
        return default() if callable(default) else None

    async def _prepare_async(self, item: BatchWriteItem) -> PreparedWrite:
        """在 IO 泳道执行同步 prepare（ingest / watermark / payload 规约）。"""
        return await self._run_blocking(_io_lane(), self._prepare, item)

    async def _execute_group(
        self, prepared: list[PreparedWrite], *, merge_extract: bool,
    ) -> list[BatchWriteOutcome]:
        outcomes = [
            BatchWriteOutcome(index=index, item=entry.item)
            for index, entry in enumerate(prepared)
        ]
        for segment in self._segments(prepared):
            indices = [prepared.index(entry) for entry in segment]
            try:
                path = segment[0].path
                if path is WritePath.DIRECT:
                    await self._execute_direct(segment, outcomes, indices)
                elif path is WritePath.MIDDLE:
                    await self._execute_middle(segment, outcomes, indices)
                else:
                    wants_merge = merge_extract and path is WritePath.INFER
                    merge = wants_merge and all(
                        entry.evolver is not None
                        and supports_grouped_extract(entry.evolver)
                        for entry in segment
                    )
                    if wants_merge and not merge and len(segment) > 1:
                        self.write_signals.record(
                            DegradeEvent.EXTRACT_UNSUPPORTED,
                            scope=_segment_scope(segment),
                            batch_size=len(segment),
                            reason="evolver.supports_grouped_extract() is False",
                        )
                    await self._execute_extract(segment, outcomes, indices, merge=merge)
            except Exception as exc:  # noqa: BLE001 —— 失败按段归集，不中断其他段
                _raise_if_cancelled(exc)
                self._fail_segment(segment, outcomes, indices, exc)
            finally:
                trace_event(
                    type(self).__name__,
                    "result",
                    {
                        "path": legacy_path_name(segment[0].path),
                        "originals": sum(len(entry.units) for entry in segment),
                        "returned": sum(len(outcomes[index].units) for index in indices),
                    },
                    level=TraceLevel.SIMPLE,
                )
        return outcomes

    @staticmethod
    def _fail_segment(
        segment: list[PreparedWrite],
        outcomes: list[BatchWriteOutcome],
        indices: list[int],
        exc: Exception,
    ) -> None:
        if not isinstance(exc, AgentMemoryError):
            logger.exception("grouped write failed for %d items", len(segment))
        for index in indices:
            _set_failure(outcomes[index], exc)

    # -- 各路径 -------------------------------------------------------------- #

    async def _execute_extract(
        self,
        segment: list[PreparedWrite],
        outcomes: list[BatchWriteOutcome],
        indices: list[int],
        *,
        merge: bool,
    ) -> None:
        first = segment[0]
        units = [unit for entry in segment for unit in entry.units]
        if merge:
            try:
                grouped = await self._run_blocking(
                    _llm_lane(), first.evolver.evolve_grouped, units
                )
            except ExtractNotCommittedError as exc:
                self.write_signals.record(
                    DegradeEvent.MERGE_FALLBACK,
                    scope=_segment_scope(segment),
                    batch_size=len(segment),
                    reason=type(exc).__name__,
                )
                await self._execute_extract_per_item(segment, outcomes, indices)
                return
            except Exception as exc:  # noqa: BLE001 —— 提交状态未知，禁止自动重放
                _raise_if_cancelled(exc)
                self.write_signals.record(
                    DegradeEvent.UNKNOWN_NO_REPLAY,
                    scope=_segment_scope(segment),
                    batch_size=len(segment),
                    reason=type(exc).__name__,
                )
                raise
            record_ingest_ids(grouped.result.created_ids)
            _distribute(segment, indices, outcomes, units, grouped, self.write_signals)
            self._trace_segment(segment, grouped.result, merged=True)
            return
        await self._execute_extract_per_item(segment, outcomes, indices)

    async def _execute_extract_per_item(
        self,
        segment: list[PreparedWrite],
        outcomes: list[BatchWriteOutcome],
        indices: list[int],
    ) -> None:
        loader = getattr(self, "_load", None)
        for index, entry in zip(indices, segment):
            if entry.evolver is None:
                raise RuntimeError(f"{type(self).__name__} requires an Evolver for extract")
            try:
                begin_ingest_write()
                result = await self._run_blocking(
                    _llm_lane(), entry.evolver.evolve, entry.units, EvolveMode.EXTRACT
                )
                record_ingest_ids(result.created_ids)
                derived = list(result.created_units)
                if not derived and loader is not None and entry.units:
                    derived = [
                        loader(entry.units[0].scope, unit_id) for unit_id in result.created_ids
                    ]
                outcomes[index].units = [unit for unit in derived if unit is not None]
                self._trace_segment([entry], result, merged=False)
            except Exception as exc:  # noqa: BLE001 —— 逐条隔离，不影响同批其他条目
                _raise_if_cancelled(exc)
                _set_failure(outcomes[index], exc)

    async def _execute_direct(
        self,
        segment: list[PreparedWrite],
        outcomes: list[BatchWriteOutcome],
        indices: list[int],
    ) -> None:
        units = [unit for entry in segment for unit in entry.units]
        classifier = segment[0].classifier
        if classifier is not None:
            await self._run_blocking(_llm_lane(), classifier.classify, units)
        begin_ingest_write()
        index_builder = segment[0].index_builder
        if (
            index_builder is not None
            and not supports_grouped_build(index_builder)
            and len(segment) > 1
        ):
            self.write_signals.record(
                DegradeEvent.BUILD_UNSUPPORTED,
                scope=_segment_scope(segment),
                batch_size=len(segment),
                reason="index_builder.supports_grouped_build() is False",
            )
        if index_builder is not None and supports_grouped_build(index_builder):
            statuses = await self._run_blocking(
                _io_lane(), index_builder.build_grouped, units
            )
            _apply_commit_statuses(segment, outcomes, indices, statuses)
            record_ingest_units(
                [unit for unit, status in zip(units, statuses)
                 if status.state is UnitCommitState.COMMITTED]
            )
            return
        for index, entry in zip(indices, segment):
            try:
                if entry.index_builder is not None:
                    await self._run_blocking(_io_lane(), entry.index_builder.build, entry.units)
                record_ingest_units(entry.units)
                outcomes[index].units = list(entry.units)
            except Exception as exc:  # noqa: BLE001 —— 单条 build 失败不连累同段其他条目
                _raise_if_cancelled(exc)
                _set_failure(outcomes[index], exc)

    async def _execute_middle(
        self,
        segment: list[PreparedWrite],
        outcomes: list[BatchWriteOutcome],
        indices: list[int],
    ) -> None:
        for index, entry in zip(indices, segment):
            try:
                outcomes[index].units = await self._execute_middle_item(entry)
            except Exception as exc:  # noqa: BLE001 —— 单条 middle 失败不连累同段其他条目
                _raise_if_cancelled(exc)
                _set_failure(outcomes[index], exc)

    # -- 引擎钩子 ------------------------------------------------------------ #

    @staticmethod
    def _prepare(item: BatchWriteItem) -> PreparedWrite:
        """引擎实现：把一条输入变成已 ingest 的 unit + 路径 + 组件。"""
        raise NotImplementedError

    @staticmethod
    async def _execute_middle_item(prepared: PreparedWrite) -> list[MemoryUnit]:
        """引擎实现：middle 子路径（提交 MiddleToLongJob）。"""
        raise NotImplementedError

    @staticmethod
    def _trace_segment(
        segment: list[PreparedWrite], result: EvolveResult, *, merged: bool,
    ) -> None:
        logger.info(
            "write extract: %s",
            metadata_for_log({"merged": merged, "items": len(segment),
                              "derived": len(result.created_ids)}),
        )


def _llm_lane():
    from jiuwen_memory.control.executor import ExecutorLane

    return ExecutorLane.LLM


def _io_lane():
    from jiuwen_memory.control.executor import ExecutorLane

    return ExecutorLane.IO


def _raise_if_cancelled(exc: Exception) -> None:
    """任务取消不是写入失败：必须原样上抛，由摄入控制器记录 cancelled 终态。"""
    if isinstance(exc, IngestCancelled):
        raise exc


def _is_infer_item(item: BatchWriteItem) -> bool:
    """本条是否走纯 infer（middle / procedural 不参与合并、也不要求合并抽取能力）。"""
    metadata = item.system_metadata or {}
    if str(metadata.get("procedural", "")).strip().lower() == "true":
        return False
    if str(metadata.get("middle", "")).strip().lower() == "true":
        return False
    return str(metadata.get("infer", "")).strip().lower() == "true"


def _set_failure(outcome: BatchWriteOutcome, exc: Exception) -> None:
    """把异常写成一条失败回执；域内异常保留类型与消息，其余按内部错误。

    非领域异常必须留下堆栈：回执对调用方屏蔽了原文，日志是唯一现场来源。
    """
    domain_error = isinstance(exc, AgentMemoryError)
    if not domain_error:
        # 只记位置与异常类型：记忆正文是隐私能力保护的数据，不能进运行日志。
        logger.exception(
            "write failed for item #%d (%s)", outcome.index, type(exc).__name__
        )
    outcome.error = str(exc) if domain_error else "unexpected write failure"
    outcome.error_type = type(exc).__name__ if domain_error else "InternalError"
    outcome.error_obj = exc


def _segment_scope(segment: list[PreparedWrite]) -> Scope | None:
    for entry in segment:
        if entry.units:
            return entry.units[0].scope
    return None


def _source_index(segment: list[PreparedWrite]) -> dict[str, list[int]]:
    mapping: dict[str, list[int]] = {}
    for position, entry in enumerate(segment):
        for unit in entry.units:
            mapping.setdefault(unit.id, []).append(position)
    return mapping


def _distribute(
    segment: list[PreparedWrite],
    indices: list[int],
    outcomes: list[BatchWriteOutcome],
    units: list[MemoryUnit],
    grouped,
    signals: WriteSignals,
) -> None:
    """按 ``GroupedExtractResult.sources`` 把派生记忆分回各条（F10 决策 6）。

    来源缺失（抽取器没标注）时整批共享，并留下可观测信号——这是唯一允许共享的情形。
    """
    source_index = _source_index(segment)
    per_item: list[list[MemoryUnit]] = [[] for _ in segment]
    shared: list[MemoryUnit] = []
    for unit in grouped.result.created_units:
        source_ids = grouped.sources.get(unit.id, [])
        targets: set[int] = set()
        for source_id in source_ids:
            targets.update(source_index.get(source_id, []))
        if not targets:
            shared.append(unit)
            continue
        for position in targets:
            per_item[position].append(unit)
    if shared:
        signals.record(
            DegradeEvent.SHARED_ATTRIBUTION,
            scope=_segment_scope(segment),
            batch_size=len(segment),
            reason=f"{len(shared)} derived units without provenance",
        )
        for position in range(len(segment)):
            per_item[position].extend(shared)
    for position, index in enumerate(indices):
        outcomes[index].units = per_item[position]


def _apply_commit_statuses(
    segment: list[PreparedWrite],
    outcomes: list[BatchWriteOutcome],
    indices: list[int],
    statuses,
) -> None:
    """把 ``UnitCommitStatus`` 按 unit id 分回输入条目。

    一个条目可能产出多条 unit：任一未提交即让该条带错误；已提交的 unit 仍出现在
    ``units`` 里，表示部分提交（F10 决策 4）。
    """
    by_unit = {status.unit_id: status for status in statuses}
    for index, entry in zip(indices, segment):
        committed: list[MemoryUnit] = []
        errors: list[Exception] = []
        unknown_ids: list[str] = []
        failed_ids: list[str] = []
        for unit in entry.units:
            status = by_unit.get(unit.id)
            if status is None:
                # 插件没给回执：不能假定已提交——按「无法确认」处理。
                unknown_ids.append(unit.id)
                continue
            if status.state is UnitCommitState.COMMITTED:
                committed.append(unit)
                if status.error is not None:
                    # 本体已提交、检索索引失败：部分提交。
                    errors.append(status.error)
                continue
            if status.state is UnitCommitState.UNKNOWN:
                unknown_ids.append(unit.id)
            else:
                failed_ids.append(unit.id)
            if status.error is not None:
                errors.append(status.error)
        outcome = outcomes[index]
        outcome.units = committed
        if not (unknown_ids or failed_ids or errors):
            continue
        detail = ""
        if unknown_ids:
            detail = f"提交状态未知的 unit: {', '.join(sorted(unknown_ids))}"
        elif failed_ids:
            detail = f"提交失败的 unit: {', '.join(sorted(failed_ids))}"
        first = errors[0] if errors else None
        message = str(first) if first is not None else "index builder reported no status"
        outcome.error = f"{message}（{detail}）" if detail else message
        if unknown_ids:
            # 单条与批量入口必须给出同一个领域异常：调用方按「先核实落盘再重试」处理。
            error: Exception = WriteIncompleteError(
                operation_id="",
                status="unknown",
                retry_action="recover_then_retry",
                message=outcome.error,
            )
        elif first is not None:
            error = first
        else:
            error = RuntimeError(outcome.error)
        outcome.error_type = type(error).__name__
        outcome.error_obj = error
