# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Event 测试输入与四层配置，均通过公开构建接口生成。"""

from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, MemoryUnit
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeProfile
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.construction.hierarchy_composer_impl.scene_pipeline import (
    SceneSegmenterOptions,
    build_scene_parent,
)
from tests.unit.construction.scene_fixtures import make_span
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE
from tests.unit.control.hierarchy_job_fixtures import HierarchyJobHarness, job_harness

EVENT_ROLES = [HierarchyRole.TIME_SPAN, HierarchyRole.SCENE, HierarchyRole.EVENT]


def make_scene(uid: str, minute: float = 0) -> MemoryUnit:
    """生成带确定性正文的 scene，并固定 id。"""
    scene = build_scene_parent(
        [make_span(f"{uid}-span", minute)], tree_home_scope=TREE_HOME_SCOPE,
        options=SceneSegmenterOptions(),
    )
    scene.id = uid
    return scene


def event_profiles(**options: str) -> dict[HierarchyKind, HierarchyComposeProfile]:
    """每分钟形成独立 scene，EventBuilder 参数由用例提供。"""
    return build_profiles({"time": {
        "parent_roles": ["time_span", "scene", "event"],
        "stage_options": {
            "TimeSpanMerger": {"gap_seconds": "1"},
            "SceneSegmenter": {"max_duration_seconds": "1"},
            "EventBuilder": options,
        },
    }})


def event_job_harness(leaves: list[MemoryUnit]) -> HierarchyJobHarness:
    """公开装配四层任务，继承真实内存读写和故障观察入口。"""
    harness = job_harness(leaves, profiles=event_profiles())
    harness.options.parent_roles = list(EVENT_ROLES)
    return harness


def read_event_path(harness: HierarchyJobHarness, original: MemoryUnit) -> list[MemoryUnit]:
    """沿完整父引用读取 snapshot、time_span、scene、event。"""
    path = [harness.read(original)]
    for _ in range(3):
        child = path[-1]
        path.append(harness.composition.read(
            child.hierarchy.resolved_parent_scope(child.scope), child.hierarchy.parent_id,
        ))
    return path
