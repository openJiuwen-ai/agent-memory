# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内部检索参数载体；公开默认值由 MemoryAPI.search 签名声明。"""

from dataclasses import dataclass
from datetime import datetime

from jiuwen_memory.common.type_def import FilterClause, FilterExpr, HierarchyKind, HierarchyRole
from jiuwen_memory.retrieval.types import DisclosureLevel


@dataclass(frozen=True)
class SearchOptions:
    """中性参数载体；API 显式传入签名默认值，关闭树开关后归一化为空结构。"""

    filters: FilterExpr | list[FilterClause] | dict | None = None
    as_of: datetime | None = None
    top_k: int = 10
    disclosure: DisclosureLevel = DisclosureLevel.L0
    with_trajectory: bool = False
    hierarchy_kind: HierarchyKind | None = None
    hierarchy_role: HierarchyRole | None = None
    span_start: datetime | None = None
    span_end: datetime | None = None
    expand_depth: int = 0
    rollup: bool = False
