# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""在公开 API 装配阶段为普通请求预设 TIME 查询，显式层级组选项保持原样。"""

from dataclasses import replace

from jiuwen_memory.api.search_options import SearchOptions
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole


def with_hierarchy_defaults(options: SearchOptions, *, enabled: bool) -> SearchOptions:
    """仅补普通请求的整组树选项，不推断调用者显式层级条件或覆盖普通参数。"""
    if not enabled:
        return options
    hierarchy_values = (
        options.hierarchy_kind, options.hierarchy_role, options.span_start, options.span_end,
    )
    if any(value is not None for value in hierarchy_values):
        return options
    # 非默认值属于显式结构请求；非法类型留给原参数校验，不掩盖 False/None 等错误。
    if type(options.expand_depth) is not int or options.expand_depth != 0:
        return options
    if not isinstance(options.rollup, bool) or options.rollup:
        return options
    return replace(
        options,
        hierarchy_kind=HierarchyKind.TIME,
        hierarchy_role=HierarchyRole.TIME_SPAN,
        rollup=True,
        expand_depth=1,
    )
