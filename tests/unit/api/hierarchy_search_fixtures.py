# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""层级查询测试的公开装配配置与确定性输入。"""

from datetime import timedelta

from jiuwen_memory.api import HierarchyKind, HierarchyRole, SearchOptions
from tests.unit.api.hierarchy_api_fixtures import START, runtime_config


class OpaqueRuntimeOption:
    """模拟必须保持身份、不能 deepcopy 的运行时插件依赖。"""

    def __deepcopy__(self, memo):
        raise TypeError(f"{type(self).__name__} cannot be deep-copied")


def hierarchy_search_options(**overrides) -> SearchOptions:
    """默认查询 snapshot，时间区间独立于事件与有效时间。"""
    values = {
        "hierarchy_kind": HierarchyKind.TIME,
        "hierarchy_role": HierarchyRole.SNAPSHOT,
        "span_start": START,
        "span_end": START + timedelta(minutes=1),
        "top_k": 10,
    }
    values.update(overrides)
    return SearchOptions(**values)


def space_search_config() -> dict:
    """用 CloudEngine 与真实空间权限验证跨空间层级检索。"""
    config = runtime_config("cloud")
    config["permission"] = {
        "default": {"target": "space_aware", "params": {"db_path": ":memory:"}},
    }
    return config


def snapshot_metadata() -> dict[str, str]:
    """摄入一条权威 snapshot，不借助内部存储写入。"""
    return {
        "hierarchy_kind": "time", "hierarchy_role": "snapshot",
        "hierarchy_span_start": START.isoformat(),
        "hierarchy_span_end": START.isoformat(),
    }
