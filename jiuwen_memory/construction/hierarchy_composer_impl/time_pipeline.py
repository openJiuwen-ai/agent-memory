# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 最小流水线：规则分组 ``snapshot → time_span``，不调用模型或存储。

按 UTC 的 span_start、t_event 与输入顺序稳定排序；会话或配置的系统上下文键变化
形成硬边界，相邻记录的开始与前条结束间隔超过阈值形成时间边界。阈值不是整组时长。
父正文保留两段结构：时间与数量表头，以及有界原文摘录；不是 LLM 语义摘要。

分组只引用输入，建父不改输入；完整流水线深拷贝叶后仅改父边。新增父 UUID 与摄入时间
不参与分组判据。这里不实现 scene/event、增量封口、Embedding 或 L0/L1 标注。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

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

DEFAULT_GAP_SECONDS = 7200
_SUPPORTED_OPTIONS = frozenset(
    {
        "gap_seconds",
        "boundary_metadata_keys",
        "carry_metadata_keys",
        "summary_max_leaves",
        "summary_max_chars_per_leaf",
        "summary_mode",
    }
)
_NON_PROPAGATED_KEYS = frozenset({"infer", "procedural", "middle"})


def as_utc(value: datetime) -> datetime:
    """统一为 UTC，朴素时间按 UTC 解读，保留微秒精度。"""
    if not isinstance(value, datetime):
        raise ValidationError("TIME pipeline 时间必须为 datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(stage_options: dict[str, str], key: str, fallback: int) -> int:
    raw = stage_options.get(key)
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        parsed = int(str(raw).strip())
    except ValueError as exc:
        raise ValidationError(f"stage_options.{key} 必须是正整数，实际 {raw!r}") from exc
    if parsed <= 0:
        raise ValidationError(f"stage_options.{key} 必须是正整数，实际 {raw!r}")
    return parsed


def _split_keys(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, str):
        raise ValidationError("metadata keys 必须是逗号分隔的字符串")
    keys: list[str] = []
    for item in raw.split(","):
        stripped = item.strip()
        if stripped and stripped not in keys:
            keys.append(stripped)
    return tuple(keys)


@dataclass(frozen=True)
class TimeSpanMergerOptions:
    """不可变的两层 TIME 分组与摘录选项，未实现选项一律拒绝。"""

    gap_seconds: int = DEFAULT_GAP_SECONDS
    boundary_metadata_keys: tuple[str, ...] = ()
    carry_metadata_keys: tuple[str, ...] = ()
    summary_max_leaves: int = 20
    summary_max_chars_per_leaf: int = 60

    def __post_init__(self) -> None:
        for name in ("gap_seconds", "summary_max_leaves", "summary_max_chars_per_leaf"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"{name} 必须是正整数，实际 {value!r}")
        for name in ("boundary_metadata_keys", "carry_metadata_keys"):
            configured = getattr(self, name)
            if not isinstance(configured, tuple):
                raise ValidationError(f"{name} 必须是不可变的字符串元组")
            for configured_key in configured:
                if not isinstance(configured_key, str) or not configured_key.strip():
                    raise ValidationError(f"{name} 不得包含空或非字符串键")

    @classmethod
    def from_stage_options(cls, stage_options: dict[str, str]) -> TimeSpanMergerOptions:
        """解析 TimeSpanMerger 的内层配置，拒绝未知键和非 structural 摘要模式。"""
        if not isinstance(stage_options, dict):
            raise ValidationError("TimeSpanMerger stage_options 必须是字典")
        unknown = set(stage_options) - _SUPPORTED_OPTIONS
        if unknown:
            raise ValidationError(f"TimeSpanMerger 不支持这些选项：{sorted(unknown)!r}")
        summary_mode = stage_options.get("summary_mode", "structural")
        if summary_mode != "structural":
            raise ValidationError("TimeSpanMerger 本阶段只支持 summary_mode=structural")
        return cls(
            gap_seconds=_positive_int(stage_options, "gap_seconds", DEFAULT_GAP_SECONDS),
            boundary_metadata_keys=_split_keys(stage_options.get("boundary_metadata_keys")),
            carry_metadata_keys=_split_keys(stage_options.get("carry_metadata_keys")),
            summary_max_leaves=_positive_int(stage_options, "summary_max_leaves", 20),
            summary_max_chars_per_leaf=_positive_int(
                stage_options, "summary_max_chars_per_leaf", 60
            ),
        )


def _validate_leaves(leaves: list[MemoryUnit]) -> None:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for leaf in leaves:
        if leaf.hierarchy.kind is not HierarchyKind.TIME:
            raise ValidationError("TIME pipeline 只接受 TIME snapshot")
        if leaf.hierarchy.role is not HierarchyRole.SNAPSHOT or leaf.hierarchy.child_ids:
            raise ValidationError("TIME pipeline 只接受没有子节点的 snapshot")
        as_utc(leaf.hierarchy.span_start)
        as_utc(leaf.hierarchy.span_end)
        if leaf.temporal.t_event is not None:
            as_utc(leaf.temporal.t_event)
        validate_ref(leaf.hierarchy, unit_id=leaf.id)
        identity = (
            leaf.scope.org,
            leaf.scope.space,
            leaf.scope.user,
            leaf.scope.agent,
            leaf.scope.session,
            leaf.id,
        )
        if identity in seen:
            raise ValidationError(f"TIME pipeline 输入包含重复节点：{identity!r}")
        seen.add(identity)


def _sort_key(indexed: tuple[int, MemoryUnit]) -> tuple[datetime, datetime, int]:
    position, unit = indexed
    event_time = unit.temporal.t_event
    event_order = as_utc(event_time) if event_time is not None else datetime.min.replace(
        tzinfo=timezone.utc
    )
    return as_utc(unit.hierarchy.span_start), event_order, position


@dataclass
class TimeSpanMerger:
    """按相邻 span 间隔、会话和配置上下文切分 snapshot。"""

    options: TimeSpanMergerOptions = field(default_factory=TimeSpanMergerOptions)

    def merge(self, leaves: list[MemoryUnit]) -> list[list[MemoryUnit]]:
        """返回稳定排序后的连续分组，不修改输入列表和叶对象。"""
        _validate_leaves(leaves)
        ordered = [unit for _, unit in sorted(enumerate(leaves), key=_sort_key)]
        if not ordered:
            return []
        groups: list[list[MemoryUnit]] = []
        current = [ordered[0]]
        for previous, following in zip(ordered, ordered[1:]):
            if self._is_boundary(previous, following):
                groups.append(current)
                current = []
            current.append(following)
        groups.append(current)
        return groups

    def _is_boundary(self, previous: MemoryUnit, following: MemoryUnit) -> bool:
        if previous.scope.session != following.scope.session:
            return True
        for metadata_key in self.options.boundary_metadata_keys:
            before = previous.system_metadata.get(metadata_key)
            after = following.system_metadata.get(metadata_key)
            if before != after:
                return True
        gap = as_utc(following.hierarchy.span_start) - as_utc(previous.hierarchy.span_end)
        return gap.total_seconds() > self.options.gap_seconds


def _shared_metadata(children: list[MemoryUnit], keys: tuple[str, ...]) -> dict[str, str]:
    shared: dict[str, str] = {}
    for metadata_key in keys:
        if metadata_key in _NON_PROPAGATED_KEYS:
            continue
        first_value = children[0].system_metadata.get(metadata_key)
        if not isinstance(first_value, str) or not first_value:
            continue
        if all(child.system_metadata.get(metadata_key) == first_value for child in children):
            shared[metadata_key] = first_value
    return shared


def _excerpt(text: str, limit: int) -> str:
    parts = [line.strip().lstrip("-").strip() for line in text.splitlines()]
    return " ".join(part for part in parts if part)[:limit]


def _structural_body(children: list[MemoryUnit], options: TimeSpanMergerOptions) -> str:
    lines = []
    for child in children[: options.summary_max_leaves]:
        lines.append(f"- {_excerpt(child.content, options.summary_max_chars_per_leaf)}")
    omitted = len(children) - options.summary_max_leaves
    if omitted > 0:
        lines.append(f"- …另有 {omitted} 条")
    return "\n".join(lines)


def _union_entities(children: list[MemoryUnit]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for child in children:
        for entity in child.entities:
            if entity not in seen:
                seen.add(entity)
                merged.append(entity)
    return merged


def build_time_span_parent(
    children: list[MemoryUnit],
    *,
    tree_home_scope: Scope,
    options: TimeSpanMergerOptions,
    extra_metadata: dict[str, str] | None = None,
) -> MemoryUnit:
    """由一组 snapshot 创建全新 time_span，覆盖直接子区间且不写 provenance。"""
    if not children:
        raise ValidationError("build_time_span_parent 需要至少一个子节点")
    _validate_leaves(children)
    span_start = min(as_utc(child.hierarchy.span_start) for child in children)
    span_end = max(as_utc(child.hierarchy.span_end) for child in children)
    lifted_keys = options.boundary_metadata_keys + options.carry_metadata_keys
    metadata = deepcopy(extra_metadata or {})
    metadata.update(_shared_metadata(children, lifted_keys))
    for transient_key in _NON_PROPAGATED_KEYS:
        metadata.pop(transient_key, None)
    header = f"{span_start.isoformat()} ~ {span_end.isoformat()}（{len(children)} 条记录）"
    return MemoryUnit(
        id=str(uuid.uuid4()),
        scope=deepcopy(tree_home_scope),
        tier=MemoryTier.EPISODIC,
        segments=[Segment(content=header), Segment(content=_structural_body(children, options))],
        temporal=Temporal(t_ingest=datetime.now(timezone.utc)),
        system_metadata=metadata,
        user_metadata=deepcopy(inherited_user_metadata(children)),
        entities=_union_entities(children),
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME,
            role=HierarchyRole.TIME_SPAN,
            child_ids=[child.id for child in children],
            child_scopes=[deepcopy(child.scope) for child in children],
            span_start=span_start,
            span_end=span_end,
        ),
    )


def run_time_pipeline(
    leaves: list[MemoryUnit],
    *,
    tree_home_scope: Scope,
    options: TimeSpanMergerOptions,
    extra_metadata: dict[str, str] | None = None,
) -> tuple[list[MemoryUnit], list[MemoryUnit]]:
    """在叶副本上构建两层 TIME 树，返回新父和保持输入顺序的叶副本。"""
    children_copies = deepcopy(leaves)
    groups = TimeSpanMerger(options).merge(children_copies)
    parents: list[MemoryUnit] = []
    for group in groups:
        parent = build_time_span_parent(
            group,
            tree_home_scope=tree_home_scope,
            options=options,
            extra_metadata=extra_metadata,
        )
        for child in group:
            child.hierarchy.parent_id = parent.id
            child.hierarchy.parent_scope = deepcopy(parent.scope)
        parents.append(parent)
    return parents, children_copies
