# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""结构查询的纯类型校验与真源判断；不推断 kind，不遍历或构建父子节点。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..errors import ValidationError
from .hierarchy import HierarchyKind, HierarchyRef, HierarchyRole, HierarchyStatus, validate_ref
from .memory import MemoryUnit


class HierarchyQueryFields(Protocol):
    """公开选项、内部请求和解析结果共同拥有的四个结构查询字段。"""

    hierarchy_kind: HierarchyKind | None
    hierarchy_role: HierarchyRole | None
    span_start: datetime | None
    span_end: datetime | None


@dataclass(frozen=True)
class HierarchyQuery:
    """已校验的单 kind 结构条件；省略 kind 时保持普通检索语义。"""

    hierarchy_kind: HierarchyKind | None = None
    hierarchy_role: HierarchyRole | None = None
    span_start: datetime | None = None
    span_end: datetime | None = None

    def __post_init__(self) -> None:
        if self.hierarchy_kind is not None and not isinstance(self.hierarchy_kind, HierarchyKind):
            raise ValidationError("hierarchy_kind 必须是 HierarchyKind")
        if self.hierarchy_role is not None and not isinstance(self.hierarchy_role, HierarchyRole):
            raise ValidationError("hierarchy_role 必须是 HierarchyRole")
        has_span = self.span_start is not None or self.span_end is not None
        if (self.hierarchy_role is not None or has_span) and self.hierarchy_kind is None:
            raise ValidationError("hierarchy_role 或结构 span 要求显式 hierarchy_kind")
        if not has_span:
            return
        if not isinstance(self.span_start, datetime) or not isinstance(self.span_end, datetime):
            raise ValidationError("结构 span_start/span_end 必须是成对的 datetime")
        beginning, ending = _as_utc(self.span_start), _as_utc(self.span_end)
        if beginning > ending:
            raise ValidationError("结构 span_start 不得晚于 span_end")
        object.__setattr__(self, "span_start", beginning)
        object.__setattr__(self, "span_end", ending)

    @classmethod
    def from_query(cls, source: HierarchyQueryFields) -> HierarchyQuery:
        """从任一边界请求捕获独立、已校验的结构条件。"""
        return cls(
            hierarchy_kind=source.hierarchy_kind,
            hierarchy_role=source.hierarchy_role,
            span_start=source.span_start,
            span_end=source.span_end,
        )

    @property
    def enabled(self) -> bool:
        """请求是否显式限定结构 kind，而不是运行时策略是否开启。"""
        return self.hierarchy_kind is not None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_expand_depth(depth: int, kind: HierarchyKind | None) -> None:
    """展开只接受非负整数深度，非零深度必须显式指定结构 kind。"""
    if type(depth) is not int or depth < 0:
        raise ValidationError("expand_depth 必须是非负整数，不能是 bool")
    if depth and kind is None:
        raise ValidationError("非零 expand_depth 要求显式 hierarchy_kind")


def matches_hierarchy(unit: MemoryUnit, query: HierarchyQuery) -> bool:
    """真源复核 kind/role/status 与闭区间；缺失或非法 TIME 区间不能匹配。"""
    if not query.enabled:
        return True
    reference = unit.hierarchy
    if not isinstance(reference, HierarchyRef) or reference.kind is not query.hierarchy_kind:
        return False
    if reference.status is not HierarchyStatus.ACTIVE:
        return False
    if not isinstance(reference.role, HierarchyRole):
        return False
    if query.hierarchy_role is not None and reference.role is not query.hierarchy_role:
        return False
    try:
        validate_ref(reference, unit_id=unit.id)
    except (ValidationError, TypeError, AttributeError):
        return False
    if query.span_start is None:
        return True
    if not isinstance(reference.span_start, datetime) or not isinstance(
        reference.span_end, datetime
    ):
        return False
    return (
        _as_utc(reference.span_start) <= query.span_end
        and _as_utc(reference.span_end) >= query.span_start
    )
