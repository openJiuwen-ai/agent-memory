# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""有序 scene → event：相邻上下文、实体重叠和可选语义相似度共同决定切点。

无时长限制，不跨过中间场景重组。实体重叠取去重集合交集除以较小集合大小；任侧
为空时不据此切分。默认所有判据关闭，整个选定范围形成一组，不证明它们属于同一任务。
PROCEDURAL 仅为 event 的分类，不表示已验证可复用技能；settle_seconds 仅供增量尾组封口。
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


@dataclass(frozen=True)
class EventBuilderOptions:
    """Event 的可选相邻判据与有界摘要配置，不设置时间窗口。"""

    boundary_metadata_keys: tuple[str, ...] = ()
    entity_overlap_threshold: float | None = None
    similarity_threshold: float | None = None
    carry_metadata_keys: tuple[str, ...] = ()
    summary_mode: str = "structural"
    summary_max_children: int = 20
    summary_max_chars_per_child: int = 300
    settle_seconds: int = 259200

    def __post_init__(self) -> None:
        for name in ("entity_overlap_threshold", "similarity_threshold"):
            threshold = getattr(self, name)
            if threshold is not None and (
                type(threshold) not in (int, float) or not math.isfinite(threshold)
                or not 0 <= threshold <= 1
            ):
                raise ValidationError(f"{name} 必须是 [0, 1] 的有限数值或 None")
        for name in ("summary_max_children", "summary_max_chars_per_child", "settle_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValidationError(f"{name} 必须是正整数")
        for name in ("boundary_metadata_keys", "carry_metadata_keys"):
            keys = getattr(self, name)
            if not isinstance(keys, tuple):
                raise ValidationError(f"{name} 必须是字符串元组")
            if any(not isinstance(key, str) or not key.strip() for key in keys):
                raise ValidationError(f"{name} 不得包含空或非字符串键")
        if self.summary_mode not in ("structural", "llm"):
            raise ValidationError("summary_mode 必须是 structural 或 llm")

    @classmethod
    def from_stage_options(cls, values: dict[str, str]) -> EventBuilderOptions:
        """严格解析分组、摘要与增量封口配置，未知键报错。"""
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValidationError("EventBuilder 配置必须是只含已支持键的字典")
        return cls(
            boundary_metadata_keys=split_keys(values.get("boundary_metadata_keys")),
            entity_overlap_threshold=_threshold(values.get("entity_overlap_threshold")),
            similarity_threshold=_threshold(values.get("similarity_threshold")),
            carry_metadata_keys=split_keys(values.get("carry_metadata_keys")),
            summary_mode=values.get("summary_mode", "structural"),
            summary_max_children=positive_int(values, "summary_max_children", 20),
            summary_max_chars_per_child=positive_int(values, "summary_max_chars_per_child", 300),
            settle_seconds=positive_int(values, "settle_seconds", 259200),
        )


def _threshold(value: str | None) -> float | None:
    if isinstance(value, bool):
        raise ValidationError("event threshold 不能是 bool")
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("event threshold 必须是数值") from exc


def _validate_scenes(scenes: list[MemoryUnit]) -> None:
    for scene in scenes:
        if scene.hierarchy.kind is not HierarchyKind.TIME or (
            scene.hierarchy.role is not HierarchyRole.SCENE
        ):
            raise ValidationError("EventBuilder 只接受 TIME scene")
        validate_ref(scene.hierarchy, unit_id=scene.id)
        as_utc(scene.hierarchy.span_start)
        as_utc(scene.hierarchy.span_end)


@dataclass
class EventBuilder:
    """相邻 scene 分组，不重写输入、不进行内容摘要。"""

    options: EventBuilderOptions = field(default_factory=EventBuilderOptions)
    embedder: Embedder | None = None

    def group(self, scenes: list[MemoryUnit]) -> list[list[MemoryUnit]]:
        """稳定按 UTC 起点排序，任一已启用判据触发即分开。"""
        _validate_scenes(scenes)
        ordered = sorted(scenes, key=lambda unit: as_utc(unit.hierarchy.span_start))
        if not ordered:
            return []
        similarities = adjacent_similarities(
            ordered, self.options.similarity_threshold, self.embedder, "event",
        )
        groups = [[ordered[0]]]
        for position in range(1, len(ordered)):
            following = ordered[position]
            if self._boundary(ordered[position - 1], following, similarities[position - 1]):
                groups.append([])
            groups[-1].append(following)
        return groups

    def _boundary(
        self, previous: MemoryUnit, following: MemoryUnit, similarity: float | None,
    ) -> bool:
        for key in self.options.boundary_metadata_keys:
            if previous.system_metadata.get(key) != following.system_metadata.get(key):
                return True
        threshold = self.options.entity_overlap_threshold
        before, after = set(previous.entities), set(following.entities)
        if threshold is not None and before and after:
            overlap = len(before & after) / min(len(before), len(after))
            if overlap < threshold:
                return True
        return similarity is not None and similarity < self.options.similarity_threshold


def build_event_parent(
    children: list[MemoryUnit], *, tree_home_scope: Scope,
    options: EventBuilderOptions, extra_metadata: dict[str, str] | None = None,
) -> MemoryUnit:
    """生成 event 的确定性摘录、完整子边及代码统计，不虚构任务元数据。"""
    if not children:
        raise ValidationError("build_event_parent 需要至少一个子节点")
    _validate_scenes(children)
    start = min(as_utc(child.hierarchy.span_start) for child in children)
    end = max(as_utc(child.hierarchy.span_end) for child in children)
    count = sum(len(child.hierarchy.child_ids) for child in children)
    header = f"{start.isoformat()} ~ {end.isoformat()}（{len(children)} 个场景，{count} 个片段）"
    lines = [f"- {excerpt(semantic_text(item), options.summary_max_chars_per_child)}"
             for item in children[:options.summary_max_children]]
    if len(children) > options.summary_max_children:
        lines.append(f"- …另有 {len(children) - options.summary_max_children} 个场景")
    metadata = deepcopy(extra_metadata or {})
    metadata.update(shared_metadata(children, options.boundary_metadata_keys
                                    + options.carry_metadata_keys))
    for transient in ("infer", "procedural", "middle"):
        metadata.pop(transient, None)
    return MemoryUnit(
        id=str(uuid.uuid4()), scope=deepcopy(tree_home_scope), tier=MemoryTier.PROCEDURAL,
        segments=[Segment(content=header), Segment(content="\n".join(lines))],
        temporal=Temporal(t_ingest=datetime.now(timezone.utc)),
        system_metadata=metadata, user_metadata=deepcopy(inherited_user_metadata(children)),
        entities=union_entities(children),
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME, role=HierarchyRole.EVENT,
            child_ids=[child.id for child in children],
            child_scopes=[deepcopy(child.scope) for child in children],
            span_start=start, span_end=end,
        ),
    )
