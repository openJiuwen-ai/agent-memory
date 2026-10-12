# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""树结构类型和 codec 测试的确定性构造辅助。"""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    Scope,
    Segment,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads

BASE = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    """返回相对固定基准的时间。"""
    return BASE + timedelta(hours=hours)


def scope(**overrides: str) -> Scope:
    """构造同一租户空间中的测试 Scope。"""
    fields: dict[str, str] = {"org": "acme", "space": "prod", "user": "zhangsan"}
    fields.update(overrides)
    return Scope(**fields)


def node(
    uid: str,
    role: HierarchyRole,
    span: tuple[datetime, datetime] | None = None,
    *,
    unit_scope: Scope | None = None,
    ref: HierarchyRef | None = None,
) -> MemoryUnit:
    """构造节点，额外结构字段用 HierarchyRef 聚合且不修改入参。"""
    source = ref if ref is not None else HierarchyRef()
    span_start, span_end = span if span is not None else (None, None)
    return MemoryUnit(
        id=uid,
        scope=unit_scope or scope(),
        segments=[Segment(content=uid)],
        hierarchy=replace(
            source,
            kind=source.kind or HierarchyKind.TIME,
            role=role,
            span_start=span_start,
            span_end=span_end,
            child_ids=list(source.child_ids),
            child_scopes=list(source.child_scopes),
        ),
    )


def hierarchy_payload(unit: MemoryUnit) -> dict | None:
    """读取实际序列化产物中的 hierarchy 段。"""
    return json.loads(dumps(unit)).get("hierarchy")


def with_hierarchy(unit: MemoryUnit, value: object) -> MemoryUnit | None:
    """替换序列化的 hierarchy 段，模拟异常或未来版本数据后读回。"""
    payload = json.loads(dumps(unit))
    payload["hierarchy"] = value
    return loads(json.dumps(payload).encode("utf-8"))
