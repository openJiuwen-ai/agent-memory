# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""公开检索选项；身份、目标 Context 与查询正文分别由入口接收。"""

from dataclasses import dataclass
from datetime import datetime

from jiuwen_memory.common.type_def import FilterClause, FilterExpr, HierarchyKind, HierarchyRole
from jiuwen_memory.retrieval.types import DisclosureLevel


@dataclass(frozen=True)
class SearchOptions:
    """统一检索选项；上卷和向下展开独立，默认均不沿树边读取。"""

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
