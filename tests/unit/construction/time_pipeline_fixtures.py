# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""TIME 最小流水线测试使用的固定时间与叶构造辅助。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    MemoryUnit,
    MetadataValueType,
    Scope,
    Segment,
)

BASE_TIME = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
TREE_HOME_SCOPE = Scope(org="acme", space="prod", user="user1", agent="agent1")


def at(minutes: float) -> datetime:
    """返回相对固定基准的时间，支持微秒边界。"""
    return BASE_TIME + timedelta(minutes=minutes)


def make_leaf(
    uid: str,
    minutes: float,
    *,
    unit_scope: Scope | None = None,
    duration: float = 0,
    metadata: dict[str, MetadataValueType] | None = None,
) -> MemoryUnit:
    """构造合法 snapshot，Scope 与元数据均不共享可变输入。"""
    actual_scope = deepcopy(unit_scope or TREE_HOME_SCOPE)
    if unit_scope is None:
        actual_scope.session = "session1"
    return MemoryUnit(
        id=uid,
        scope=actual_scope,
        segments=[Segment(content=f"{uid} 的内容")],
        system_metadata=deepcopy(metadata or {}),
        hierarchy=HierarchyRef(
            kind=HierarchyKind.TIME,
            role=HierarchyRole.SNAPSHOT,
            span_start=at(minutes),
            span_end=at(minutes + duration),
        ),
    )


def group_ids(groups: list[list[MemoryUnit]]) -> list[list[str]]:
    """通过公开节点字段查看分组，不读取生产算子的保护成员。"""
    return [[item.id for item in group] for group in groups]
