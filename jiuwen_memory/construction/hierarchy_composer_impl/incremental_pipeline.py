# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 单层增量：先用真源子树还原确定性摘录，再分组、封口与增强新父。

事件静默期是运行策略而不是语义终止证明。所有尾组都使用严格大于阈值；下层尚未
完成的最早起点阻挡上层封口，避免上层水位越过下一轮才会生成的子节点。
还原仅作用于临时视图，已落盘输入的正文、层摘要和直接子边不被重写。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeRequest

from .event_pipeline import EventBuilder, EventBuilderOptions, build_event_parent
from .parent_enrichment import (
    HierarchyModelDependencies,
    ParentSummaryOptions,
    annotate_parents,
    summarize_parent,
)
from .scene_pipeline import SceneSegmenter, SceneSegmenterOptions, build_scene_parent
from .time_pipeline import TimeSpanMerger, TimeSpanMergerOptions, as_utc, build_time_span_parent


@dataclass
class IncrementalBatch:
    """待交付新父、实际改父边的输入和未完成的最早时间。"""

    parents: list[MemoryUnit]
    children: list[MemoryUnit]
    pending_before: datetime | None


@dataclass
class IncrementalTimePipeline:
    """消费已经完成类型与子树完整性校验的请求。"""

    merger: TimeSpanMergerOptions
    segmenter: SceneSegmenterOptions
    event_builder: EventBuilderOptions
    models: HierarchyModelDependencies

    def build(self, request: HierarchyComposeRequest) -> IncrementalBatch:
        """只对确定封口的组生成父摘要，保留未封口输入以供下轮处理。"""
        role = request.options.parent_roles[0]
        actual = {_key(unit): unit for unit in request.leaves}
        indexed = {**actual, **{_key(unit): unit for unit in request.incremental.supporting_units}}
        views = [self._structural_view(unit, indexed) for unit in request.leaves]
        groups = self._groups(views, role)
        settled = self._settled(groups, request)
        parents, children = [], []
        attached = set()
        for group in settled:
            parent = self._parent(group, request)
            originals = [deepcopy(actual[_key(view)]) for view in group]
            for original in originals:
                original.hierarchy.parent_id = parent.id
                original.hierarchy.parent_scope = deepcopy(parent.scope)
                attached.add(_key(original))
            summarize_parent(parent, originals, self._summary_options(role), self.models.llm)
            parents.append(parent)
            children.extend(originals)
        annotate_parents(parents, self.models.layer_annotator)
        pending = [as_utc(unit.hierarchy.span_start) for unit in request.leaves
                   if _key(unit) not in attached]
        if request.incremental.ready_before is not None:
            pending.append(as_utc(request.incremental.ready_before))
        return IncrementalBatch(parents, children, min(pending) if pending else None)

    def _structural_view(self, unit: MemoryUnit, indexed: dict[tuple, MemoryUnit]) -> MemoryUnit:
        if unit.hierarchy.role is HierarchyRole.SNAPSHOT:
            return deepcopy(unit)
        children = []
        for position, uid in enumerate(unit.hierarchy.child_ids):
            scope = unit.hierarchy.child_scope_at(position, unit.scope)
            key = (scope.org, scope.space, scope.user, scope.agent, scope.session, uid)
            children.append(self._structural_view(indexed[key], indexed))
        if unit.hierarchy.role is HierarchyRole.TIME_SPAN:
            structural = build_time_span_parent(children, tree_home_scope=unit.scope,
                                                options=self.merger)
        else:
            structural = build_scene_parent(children, tree_home_scope=unit.scope,
                                            options=self.segmenter)
        view = deepcopy(unit)
        view.segments = structural.segments
        return view

    def _groups(self, inputs: list[MemoryUnit], role: HierarchyRole) -> list[list[MemoryUnit]]:
        if role is HierarchyRole.TIME_SPAN:
            return TimeSpanMerger(self.merger).merge(inputs)
        if role is HierarchyRole.SCENE:
            return SceneSegmenter(self.segmenter, self.models.embedder).segment(inputs)
        return EventBuilder(self.event_builder, self.models.embedder).group(inputs)

    def _settled(
        self, groups: list[list[MemoryUnit]], request: HierarchyComposeRequest,
    ) -> list[list[MemoryUnit]]:
        if not groups:
            return []
        role = request.options.parent_roles[0]
        seconds = {
            HierarchyRole.TIME_SPAN: self.merger.gap_seconds,
            HierarchyRole.SCENE: self.segmenter.max_duration_seconds,
            HierarchyRole.EVENT: self.event_builder.settle_seconds,
        }[role]
        now = as_utc(request.incremental.settle_at)
        tail_end = max(as_utc(unit.hierarchy.span_end) for unit in groups[-1])
        limit = len(groups) if (now - tail_end).total_seconds() > seconds else len(groups) - 1
        accepted = []
        frontier = request.incremental.ready_before
        for group in groups[:limit]:
            end = max(as_utc(unit.hierarchy.span_end) for unit in group)
            if end > now or (frontier is not None and end >= as_utc(frontier)):
                break
            accepted.append(group)
        return groups[:_safe_prefix_length(groups, len(accepted), frontier)]

    def _parent(self, children: list[MemoryUnit], request: HierarchyComposeRequest) -> MemoryUnit:
        role = request.options.parent_roles[0]
        if role is HierarchyRole.TIME_SPAN:
            builder, options = build_time_span_parent, self.merger
        elif role is HierarchyRole.SCENE:
            builder, options = build_scene_parent, self.segmenter
        else:
            builder, options = build_event_parent, self.event_builder
        return builder(children, tree_home_scope=request.options.tree_home_scope,
                       options=options, extra_metadata=request.options.metadata)

    def _summary_options(self, role: HierarchyRole) -> ParentSummaryOptions:
        if role is HierarchyRole.TIME_SPAN:
            return ParentSummaryOptions(self.merger.summary_mode, self.merger.summary_max_leaves,
                                        self.merger.summary_max_chars_per_leaf)
        options = self.segmenter if role is HierarchyRole.SCENE else self.event_builder
        return ParentSummaryOptions(options.summary_mode, options.summary_max_children,
                                    options.summary_max_chars_per_child)


class _NodeKey(NamedTuple):
    org: str
    space: str
    user: str
    agent: str
    session: str
    unit_id: str


def _key(unit: MemoryUnit) -> _NodeKey:
    scope = unit.scope
    return _NodeKey(scope.org, scope.space, scope.user, scope.agent, scope.session, unit.id)


def _safe_prefix_length(
    groups: list[list[MemoryUnit]], limit: int, frontier: datetime | None,
) -> int:
    # 时间区间可重叠；已封口组的最大结束不能越过任何待定输入的起点。
    bounds = []
    for group in groups:
        bounds.append((min(as_utc(unit.hierarchy.span_start) for unit in group),
                       max(as_utc(unit.hierarchy.span_end) for unit in group)))
    while limit:
        pending = [start for start, _ in bounds[limit:]]
        if frontier is not None:
            pending.append(as_utc(frontier))
        if not pending:
            return limit
        boundary = min(pending)
        blocked = next((index for index, (_, end) in enumerate(bounds[:limit])
                        if end >= boundary), None)
        if blocked is None:
            return limit
        limit = blocked
    return 0
