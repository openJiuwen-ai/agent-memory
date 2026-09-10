# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""有序 time_span → scene：上下文/结束信号/累计跨度/可选相邻余弦切分。

session 不作 scene 边界；默认累计跨度为 86400 秒（不是自然日边界）。相似度默认
关闭，启用后必须显式注入 Embedder；向量失败或非法时在任何持久化之前拒绝建树。
此处仅消费确定性摘录，不消费 LLM 摘要，不拆分已经原子化的单个 time_span。
"""

from __future__ import annotations

import math
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jiuwen_memory.common.embedder import Embedder
from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryTier,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
    inherited_user_metadata,
    validate_ref,
)

from .grouping_support import adjacent_similarities, semantic_text
from .time_pipeline import (
    as_utc,
    excerpt,
    positive_int,
    shared_metadata,
    split_keys,
    union_entities,
)

DEFAULT_MAX_DURATION_SECONDS = 86400


@dataclass(frozen=True)
class SceneSegmenterOptions:
    """不可变切分配置；缺省不猜主题模型或业务结束信号键。"""

    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS
    similarity_threshold: float | None = None
    boundary_metadata_keys: tuple[str, ...] = ()
    end_signal_metadata_keys: tuple[str, ...] = ()
    carry_metadata_keys: tuple[str, ...] = ()
    summary_mode: str = "structural"
    summary_max_children: int = 20
    summary_max_chars_per_child: int = 80

    def __post_init__(self) -> None:
        for name in ("max_duration_seconds", "summary_max_children", "summary_max_chars_per_child"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValidationError(f"{name} 必须是正整数")
        threshold = self.similarity_threshold
        if threshold is not None and (
            type(threshold) not in (int, float) or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValidationError("similarity_threshold 必须是 [0, 1] 的有限数值或 None")
        for name in ("boundary_metadata_keys", "end_signal_metadata_keys", "carry_metadata_keys"):
            keys = getattr(self, name)
            if not isinstance(keys, tuple):
                raise ValidationError(f"{name} 必须是字符串元组")
            if any(not isinstance(key, str) or not key.strip() for key in keys):
                raise ValidationError(f"{name} 不得包含空或非字符串键")
        if self.summary_mode not in ("structural", "llm"):
            raise ValidationError("summary_mode 必须是 structural 或 llm")

    @classmethod
    def from_stage_options(cls, values: dict[str, str]) -> SceneSegmenterOptions:
        """解析 SceneSegmenter 内层参数并拒绝未实现选项。"""
        supported = set(cls.__dataclass_fields__)
        if not isinstance(values, dict) or set(values) - supported:
            raise ValidationError("SceneSegmenter 配置必须是只含已支持键的字典")
        raw_threshold = values.get("similarity_threshold")
        try:
            threshold = None if raw_threshold in (None, "") else float(raw_threshold)
        except (TypeError, ValueError) as exc:
            raise ValidationError("similarity_threshold 必须是数值") from exc
        if isinstance(raw_threshold, bool):
            raise ValidationError("similarity_threshold 不能是 bool")
        return cls(
            max_duration_seconds=positive_int(values, "max_duration_seconds", 86400),
            similarity_threshold=threshold,
            boundary_metadata_keys=split_keys(values.get("boundary_metadata_keys")),
            end_signal_metadata_keys=split_keys(values.get("end_signal_metadata_keys")),
            carry_metadata_keys=split_keys(values.get("carry_metadata_keys")),
            summary_mode=values.get("summary_mode", "structural"),
            summary_max_children=positive_int(values, "summary_max_children", 20),
            summary_max_chars_per_child=positive_int(values, "summary_max_chars_per_child", 80),
        )


def _validate_spans(spans: list[MemoryUnit]) -> None:
    for span in spans:
        if span.hierarchy.kind is not HierarchyKind.TIME or (
            span.hierarchy.role is not HierarchyRole.TIME_SPAN
        ):
            raise ValidationError("SceneSegmenter 只接受 TIME time_span")
        validate_ref(span.hierarchy, unit_id=span.id)
        as_utc(span.hierarchy.span_start)
        as_utc(span.hierarchy.span_end)


@dataclass
class _SceneWindow:
    previous: MemoryUnit
    start: datetime
    end: datetime


@dataclass
class SceneSegmenter:
    """相邻 time_span 形成 scene，先确定结构后由其它组件生成语义摘要。"""

    options: SceneSegmenterOptions = field(default_factory=SceneSegmenterOptions)
    embedder: Embedder | None = None

    def segment(self, spans: list[MemoryUnit]) -> list[list[MemoryUnit]]:
        """稳定排序并返回分组，不修改节点；单个超长 time_span 保持原子性。"""
        _validate_spans(spans)
        ordered = sorted(spans, key=lambda unit: as_utc(unit.hierarchy.span_start))
        if not ordered:
            return []
        similarities = adjacent_similarities(
            ordered, self.options.similarity_threshold, self.embedder, "scene",
        )
        groups = [[ordered[0]]]
        window = _SceneWindow(ordered[0], as_utc(ordered[0].hierarchy.span_start),
                              as_utc(ordered[0].hierarchy.span_end))
        for position in range(1, len(ordered)):
            following = ordered[position]
            following_end = as_utc(following.hierarchy.span_end)
            if self._boundary(window, following, similarities[position - 1]):
                groups.append([])
                window.start = as_utc(following.hierarchy.span_start)
                window.end = following_end
            else:
                window.end = max(window.end, following_end)
            window.previous = following
            groups[-1].append(following)
        return groups

    def _boundary(
        self, window: _SceneWindow, following: MemoryUnit, similarity: float | None,
    ) -> bool:
        previous = window.previous
        for key in self.options.boundary_metadata_keys:
            if previous.system_metadata.get(key) != following.system_metadata.get(key):
                return True
        if any(
            previous.system_metadata.get(signal) for signal in self.options.end_signal_metadata_keys
        ):
            return True
        end = max(window.end, as_utc(following.hierarchy.span_end))
        duration = end - window.start
        if duration.total_seconds() > self.options.max_duration_seconds:
            return True
        return similarity is not None and similarity < self.options.similarity_threshold


def build_scene_parent(
    children: list[MemoryUnit], *, tree_home_scope: Scope,
    options: SceneSegmenterOptions, extra_metadata: dict[str, str] | None = None,
) -> MemoryUnit:
    """生成 scene 的确定性摘录与代码统计，子边由流水线统一挂接。"""
    if not children:
        raise ValidationError("build_scene_parent 需要至少一个子节点")
    _validate_spans(children)
    start = min(as_utc(child.hierarchy.span_start) for child in children)
    end = max(as_utc(child.hierarchy.span_end) for child in children)
    count = sum(len(child.hierarchy.child_ids) for child in children)
    header = f"{start.isoformat()} ~ {end.isoformat()}（{len(children)} 个片段，{count} 条记录）"
    lines = [f"- {excerpt(semantic_text(item), options.summary_max_chars_per_child)}"
             for item in children[:options.summary_max_children]]
    if len(children) > options.summary_max_children:
        lines.append(f"- …另有 {len(children) - options.summary_max_children} 个片段")
    metadata = deepcopy(extra_metadata or {})
    metadata.update(shared_metadata(children, options.boundary_metadata_keys
                                    + options.carry_metadata_keys))
    for transient in ("infer", "procedural", "middle"):
        metadata.pop(transient, None)
    return MemoryUnit(
        id=str(uuid.uuid4()), scope=deepcopy(tree_home_scope), tier=MemoryTier.EPISODIC,
        segments=[Segment(content=header), Segment(content="\n".join(lines))],
        temporal=Temporal(t_ingest=datetime.now(timezone.utc)),
        system_metadata=metadata, user_metadata=deepcopy(inherited_user_metadata(children)),
        entities=union_entities(children),
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME, role=HierarchyRole.SCENE,
            child_ids=[child.id for child in children],
            child_scopes=[deepcopy(child.scope) for child in children],
            span_start=start, span_end=end,
        ),
    )
