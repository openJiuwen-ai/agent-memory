# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 两/三/四层流水线：先固定结构，再自下向上摘要，最后统一标注父 L0/L1。

scene 的上下文/结束信号下传给 TimeSpanMerger，避免在底层合并时丢失切点；结束信号
仅上提组尾的原值；event 上下文键逐层下传。返回父顺序为 event → scene → time_span，
便于 Composer 在切换 snapshot 父边之前从根向下保存所有新父。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyRole, MemoryUnit, Scope
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    HierarchyComposeRequest,
)

from .event_pipeline import EventBuilder, EventBuilderOptions, build_event_parent
from .incremental_pipeline import IncrementalBatch, IncrementalTimePipeline
from .parent_enrichment import (
    HierarchyModelDependencies,
    ParentSummaryOptions,
    annotate_parents,
    summarize_parent,
)
from .scene_pipeline import SceneSegmenter, SceneSegmenterOptions, build_scene_parent
from .time_pipeline import TimeSpanMergerOptions, run_time_pipeline


@dataclass
class TimeHierarchyPipeline:
    """无存储的结构及内容流水线，配置依赖在装配期验证。"""

    merger: TimeSpanMergerOptions = field(default_factory=TimeSpanMergerOptions)
    segmenter: SceneSegmenterOptions = field(default_factory=SceneSegmenterOptions)
    models: HierarchyModelDependencies = field(default_factory=HierarchyModelDependencies)
    event_builder: EventBuilderOptions = field(default_factory=EventBuilderOptions)

    def validate_dependencies(self) -> None:
        """启用模型判据/摘要时要求对应依赖，不能静默换用占位模型。"""
        for stage, config in (("scene", self.segmenter), ("event", self.event_builder)):
            if config.similarity_threshold is not None and self.models.embedder is None:
                raise ValidationError(f"{stage} similarity_threshold 要求显式配置 embedder")
        needs_llm = "llm" in (
            self.merger.summary_mode, self.segmenter.summary_mode, self.event_builder.summary_mode,
        )
        if needs_llm and self.models.llm is None:
            raise ValidationError("summary_mode=llm 要求显式配置 llm")

    def build(
        self, leaves: list[MemoryUnit], options: HierarchyComposeOptions,
    ) -> tuple[list[MemoryUnit], list[MemoryUnit]]:
        """生成全部父和叶副本，叶只改 hierarchy，模型仅增强新父内容。"""
        self.validate_dependencies()
        scene_enabled = HierarchyRole.SCENE in options.parent_roles
        event_enabled = HierarchyRole.EVENT in options.parent_roles
        segmenter = self._effective_segmenter() if event_enabled else self.segmenter
        merger = self._effective_merger(segmenter) if scene_enabled else self.merger
        spans, children = run_time_pipeline(
            leaves, tree_home_scope=options.tree_home_scope, options=merger,
            extra_metadata=options.metadata,
        )
        scenes = self._build_scenes(spans, options, segmenter) if scene_enabled else []
        events = self._build_events(scenes, options) if event_enabled else []
        self._summarize(spans, children, ParentSummaryOptions(
            merger.summary_mode, merger.summary_max_leaves, merger.summary_max_chars_per_leaf,
        ))
        self._summarize(scenes, spans, ParentSummaryOptions(
            self.segmenter.summary_mode, self.segmenter.summary_max_children,
            self.segmenter.summary_max_chars_per_child,
        ))
        self._summarize(events, scenes, ParentSummaryOptions(
            self.event_builder.summary_mode, self.event_builder.summary_max_children,
            self.event_builder.summary_max_chars_per_child,
        ))
        parents = [*events, *scenes, *spans]
        annotate_parents(parents, self.models.layer_annotator)
        return parents, children

    def build_incremental(
        self, request: HierarchyComposeRequest, chain: tuple[HierarchyRole, ...],
    ) -> IncrementalBatch:
        """复用 profile 有效配置，仅构建一层已封口分组，旧子内容保持不变。"""
        self.validate_dependencies()
        segmenter = self._effective_segmenter() if HierarchyRole.EVENT in chain else self.segmenter
        merger = self._effective_merger(segmenter) if HierarchyRole.SCENE in chain else self.merger
        pipeline = IncrementalTimePipeline(merger, segmenter, self.event_builder, self.models)
        return pipeline.build(request)

    def _effective_segmenter(self) -> SceneSegmenterOptions:
        return replace(
            self.segmenter,
            boundary_metadata_keys=tuple(dict.fromkeys(
                self.segmenter.boundary_metadata_keys + self.event_builder.boundary_metadata_keys,
            )),
            carry_metadata_keys=tuple(dict.fromkeys(
                self.segmenter.carry_metadata_keys + self.event_builder.carry_metadata_keys,
            )),
        )

    def _effective_merger(self, segmenter: SceneSegmenterOptions) -> TimeSpanMergerOptions:
        return replace(
            self.merger,
            boundary_metadata_keys=tuple(dict.fromkeys(
                self.merger.boundary_metadata_keys + segmenter.boundary_metadata_keys,
            )),
            carry_metadata_keys=tuple(dict.fromkeys(
                self.merger.carry_metadata_keys + segmenter.carry_metadata_keys,
            )),
            end_signal_metadata_keys=tuple(dict.fromkeys(
                self.merger.end_signal_metadata_keys + segmenter.end_signal_metadata_keys,
            )),
        )

    def _build_scenes(
        self, spans: list[MemoryUnit], options: HierarchyComposeOptions,
        segmenter: SceneSegmenterOptions,
    ) -> list[MemoryUnit]:
        parents = []
        for group in SceneSegmenter(segmenter, self.models.embedder).segment(spans):
            parent = build_scene_parent(
                group, tree_home_scope=options.tree_home_scope,
                options=segmenter, extra_metadata=options.metadata,
            )
            for child in group:
                child.hierarchy.parent_id = parent.id
                child.hierarchy.parent_scope = deepcopy(parent.scope)
            parents.append(parent)
        return parents

    def _build_events(
        self, scenes: list[MemoryUnit], options: HierarchyComposeOptions,
    ) -> list[MemoryUnit]:
        parents = []
        for group in EventBuilder(self.event_builder, self.models.embedder).group(scenes):
            parent = build_event_parent(
                group, tree_home_scope=options.tree_home_scope,
                options=self.event_builder, extra_metadata=options.metadata,
            )
            for child in group:
                child.hierarchy.parent_id = parent.id
                child.hierarchy.parent_scope = deepcopy(parent.scope)
            parents.append(parent)
        return parents

    def _summarize(
        self, parents: list[MemoryUnit], children: list[MemoryUnit], options: ParentSummaryOptions,
    ) -> None:
        indexed = {_key(child.scope, child.id): child for child in children}
        for parent in parents:
            ordered = [indexed[_key(parent.hierarchy.child_scope_at(position, parent.scope), uid)]
                       for position, uid in enumerate(parent.hierarchy.child_ids)]
            summarize_parent(parent, ordered, options, self.models.llm)


class _NodeKey(NamedTuple):
    org: str
    space: str
    user: str
    agent: str
    session: str
    unit_id: str


def _key(scope: Scope, uid: str) -> _NodeKey:
    return _NodeKey(scope.org, scope.space, scope.user, scope.agent, scope.session, uid)
