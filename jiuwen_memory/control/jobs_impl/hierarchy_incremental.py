# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""增量取数：复用完整树校验，按单层水位拒绝迟到输入，不截断树或扫描分页。

水位读取当前 home 全部 ACTIVE 同角色父，不受 lookback 截断。未挂父输入若早于
窗口或不晚于既有水位，本轮要求显式重建；不会忽略后继续推进上层。完整旧树读取
用于核对边与还原结构摘录，不意味着重新生成已有父。
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from jiuwen_memory.common.errors import ValidationError
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
    collect_candidates,
    node_key,
    read_scope_units,
    scope_key,
)
from jiuwen_memory.storage.kv import KVStore


@dataclass
class IncrementalSelection:
    """本层待挂父输入、只读证据、下层需等待的边界和显式重建诊断。"""

    inputs: list[MemoryUnit]
    supporting_units: list[MemoryUnit]
    pending_before: datetime | None = None
    needs_rebuild_count: int = 0


def collect_incremental(
    kv: KVStore, options: HierarchyComposeOptions, chain: tuple[HierarchyRole, ...],
    limits: HierarchyJobLimits,
) -> IncrementalSelection:
    """水位与候选都完整读取，任何迟到/过窗输入阻挡当前 home 的自动推进。"""
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
    needs_rebuild = 0
    pending = []
    for selected_scope in scopes.values():
        for unit in read_scope_units(kv, selected_scope, limits.page_size):
            if not _active(unit) or unit.hierarchy.role is not options.leaf_role:
                continue
            if unit.hierarchy.parent_id:
                continue
            validate_ref(unit.hierarchy, unit_id=unit.id)
            if options.leaf_role is not HierarchyRole.SNAPSHOT and unit.scope != home:
                continue
            start, end = utc(unit.hierarchy.span_start), utc(unit.hierarchy.span_end)
            if start < utc(options.span_start) or (watermark is not None and start <= watermark):
                needs_rebuild += 1
            elif end > utc(options.span_end):
                pending.append(start)
            else:
                initial[node_key(unit)] = unit
            if len(initial) + len(pending) + needs_rebuild > limits.max_leaves:
                raise ValidationError("incremental inputs exceed max_leaves; rebuild required")
    if needs_rebuild or not initial:
        return IncrementalSelection([], [], min(pending) if pending else None, needs_rebuild)
    whole_tree = deepcopy(options)
    whole_tree.leaf_role = HierarchyRole.SNAPSHOT
    whole_tree.parent_roles = list(chain)
    complete = collect_candidates(kv, home, whole_tree, limits)
    nodes = {**complete.leaves, **complete.parents}
    inputs, support = [], []
    for key, unit in nodes.items():
        if key in initial:
            if unit != initial[key]:
                raise ValidationError("incremental input changed during collection")
            inputs.append(unit)
        else:
            support.append(unit)
    if len(inputs) != len(initial):
        raise ValidationError("incremental inputs changed or disappeared during collection")
    if observed_parents != _home_parents(kv, options, limits):
        raise ValidationError("incremental watermark changed during collection")
    return IncrementalSelection(inputs, support, min(pending) if pending else None)


def _home_parents(
    kv: KVStore, options: HierarchyComposeOptions, limits: HierarchyJobLimits,
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
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _active(unit: MemoryUnit) -> bool:
    return (unit.hierarchy.kind is HierarchyKind.TIME
            and unit.hierarchy.status is HierarchyStatus.ACTIVE
            and unit.lifecycle is LifecycleState.ACTIVE)
