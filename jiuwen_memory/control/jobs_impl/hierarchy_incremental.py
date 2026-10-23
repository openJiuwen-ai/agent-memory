# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""增量取数：完整水位扫描，跳过迟到输入/过窗叶子，按选中根读取完整子树。

lookback 只筛选未挂父 Snapshot，中间节点继续等待上层；所有层仍跳过不晚于
目标父水位的输入。被跳过节点只记录诊断，不阻挡正常候选，也不修改既有树。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.time_utils import as_utc
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    MemoryUnit,
    validate_ref,
)
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeOptions
from jiuwen_memory.control.evolution.validation import scope_contains
from jiuwen_memory.control.jobs_impl.hierarchy_candidates import (
    HierarchyJobLimits,
    collect_root_subtrees,
    has_incomplete_leaf_time,
    node_key,
    read_scope_units,
    scope_key,
    validate_snapshot_links,
)
from jiuwen_memory.storage.domain_store import DomainStore
from jiuwen_memory.storage.kv import KVStore

MAX_SKIP_SAMPLES = 20


@dataclass
class IncrementalSelection:
    """本层待挂父输入、完整证据、待封口边界和有限跳过诊断。"""

    inputs: list[MemoryUnit]
    supporting_units: list[MemoryUnit]
    pending_before: datetime | None = None
    skipped_incomplete_time_count: int = 0
    skipped_late_count: int = 0
    skipped_outside_window_count: int = 0
    skipped_samples: list[dict[str, object]] = field(default_factory=list)

    @property
    def needs_rebuild_count(self) -> int:
        """兼容旧诊断键：需要显式补建的跳过数，不再表示整轮失败。"""
        return self.skipped_late_count + self.skipped_outside_window_count

    def skip(self, unit: MemoryUnit, reason: str) -> None:
        """原因互斥：迟到优先于过窗；只保留有界样例，不落持久化跳过标记。"""
        if reason == "incomplete_time":
            self.skipped_incomplete_time_count += 1
        elif reason == "late":
            self.skipped_late_count += 1
        else:
            self.skipped_outside_window_count += 1
        if len(self.skipped_samples) < MAX_SKIP_SAMPLES:
            self.skipped_samples.append({
                "id": unit.id, "scope": asdict(unit.scope),
                "role": unit.hierarchy.role.value if unit.hierarchy.role else None,
                "span_start": (utc(unit.hierarchy.span_start).isoformat()
                               if unit.hierarchy.span_start is not None else None),
                "span_end": (utc(unit.hierarchy.span_end).isoformat()
                             if unit.hierarchy.span_end is not None else None),
                "reason": reason,
            })


def collect_incremental(
    kv: KVStore | DomainStore, options: HierarchyComposeOptions, chain: tuple[HierarchyRole, ...],
    limits: HierarchyJobLimits,
) -> IncrementalSelection:
    """完整读取水位与候选；跳过时间异常，保留正常输入及下层待封口边界。"""
    home = options.tree_home_scope
    scopes = {scope_key(home): home}
    for candidate_scope in kv.scopes():
        if scope_contains(home, candidate_scope):
            scopes[scope_key(candidate_scope)] = candidate_scope
    observed_parents = _home_parents(kv, options, limits)
    watermark = None
    for parent in observed_parents.values():
        validate_ref(parent.hierarchy, unit_id=parent.id)
        if parent.hierarchy.role is options.parent_roles[0]:
            end = utc(parent.hierarchy.span_end)
            watermark = end if watermark is None else max(watermark, end)
        if parent.hierarchy.role not in chain:
            raise ValidationError("existing hierarchy exceeds profile; explicit rebuild required")
    initial = {}
    selection = IncrementalSelection([], [])
    pending = []
    for selected_scope in scopes.values():
        for unit in read_scope_units(kv, selected_scope, limits.page_size):
            if options.leaf_role is HierarchyRole.SNAPSHOT and has_incomplete_leaf_time(unit):
                selection.skip(unit, "incomplete_time")
                continue
            if not _active(unit) or unit.hierarchy.role is not options.leaf_role:
                continue
            validate_ref(unit.hierarchy, unit_id=unit.id)
            if unit.hierarchy.role is HierarchyRole.SNAPSHOT:
                validate_snapshot_links(unit)
            if unit.hierarchy.parent_id:
                continue
            if options.leaf_role is not HierarchyRole.SNAPSHOT and unit.scope != home:
                continue
            start, end = utc(unit.hierarchy.span_start), utc(unit.hierarchy.span_end)
            if watermark is not None and start <= watermark:
                selection.skip(unit, "late")
            elif options.leaf_role is HierarchyRole.SNAPSHOT and start < utc(options.span_start):
                selection.skip(unit, "outside_window")
            elif end > utc(options.span_end):
                pending.append(start)
            else:
                initial[node_key(unit)] = unit
            if len(initial) + len(pending) > limits.max_leaves:
                raise ValidationError("incremental inputs exceed max_leaves; rebuild required")
    selection.pending_before = min(pending) if pending else None
    if initial:
        complete = collect_root_subtrees(kv, scopes, list(initial.values()), options, limits)
        nodes = {**complete.leaves, **complete.parents}
        try:
            selection.inputs = [nodes[key] for key in initial]
        except KeyError as error:
            raise ValidationError(
                "incremental subtree collection omitted a selected root"
            ) from error
        selection.supporting_units = [unit for key, unit in nodes.items() if key not in initial]
    if observed_parents != _home_parents(kv, options, limits):
        raise ValidationError("incremental watermark changed during collection")
    return selection


def _home_parents(
    kv: KVStore | DomainStore, options: HierarchyComposeOptions, limits: HierarchyJobLimits,
) -> dict[tuple, MemoryUnit]:
    result = {}
    for node in read_scope_units(kv, options.tree_home_scope, limits.page_size):
        if _active(node) and node.hierarchy.role is not HierarchyRole.SNAPSHOT:
            result[node_key(node)] = node
            if len(result) > 3 * limits.max_leaves:
                raise ValidationError("incremental watermark parents exceed 3 * max_leaves")
    return result


def utc(value: datetime) -> datetime:
    """规范化时间；与显式任务一致，naive datetime 按 UTC 解释。"""
    if not isinstance(value, datetime):
        raise ValidationError("hierarchy time must be datetime")
    return as_utc(value)


def _active(unit: MemoryUnit) -> bool:
    return (unit.hierarchy.kind is HierarchyKind.TIME
            and unit.hierarchy.status is HierarchyStatus.ACTIVE
            and unit.lifecycle is LifecycleState.ACTIVE)
