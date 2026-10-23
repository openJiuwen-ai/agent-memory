# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Event 测试输入与四层配置，均通过公开构建接口生成。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, MemoryUnit
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeProfile
from jiuwen_memory.construction.hierarchy_composer_impl.profile_config import build_profiles
from jiuwen_memory.construction.hierarchy_composer_impl.scene_pipeline import (
    SceneSegmenterOptions,
    build_scene_parent,
)
from tests.unit.construction.scene_fixtures import make_span
from tests.unit.construction.time_pipeline_fixtures import TREE_HOME_SCOPE

if TYPE_CHECKING:
    from tests.unit.control.hierarchy_job_fixtures import HierarchyJobHarness


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


def summary_grouping_case(
    role: HierarchyRole, *, fallback: bool = False,
) -> tuple[dict[HierarchyKind, HierarchyComposeProfile], list[str | Exception]]:
    """下层生成长摘要，上层仅按 0.65 相邻语义分组；可模拟直接子摘要失败。"""
    profiles = event_profiles()
    stages = profiles[HierarchyKind.TIME].stage_options
    stages["TimeSpanMerger"]["summary_mode"] = "llm"
    responses: list[str | Exception] = [
        json.dumps({"summary": "连续活动" * 30 + f"末尾人物{i}"}, ensure_ascii=False)
        for i in range(2)
    ]
    if role is HierarchyRole.EVENT:
        stages["SceneSegmenter"]["summary_mode"] = "llm"
        responses += [json.dumps({"goal": "旅行", "actions": "完整场景" * 80 + f"末尾日期{i}",
                                  "outcome": "完成"}, ensure_ascii=False) for i in range(2)]
        stages["EventBuilder"]["similarity_threshold"] = "0.65"
    else:
        stages["SceneSegmenter"].update(similarity_threshold="0.65", max_duration_seconds="3600")
    if fallback:
        responses[-2:] = ["invalid json", RuntimeError("summary unavailable")]
    return profiles, responses


def read_event_path(harness: HierarchyJobHarness, original: MemoryUnit) -> list[MemoryUnit]:
    """沿完整父引用读取 snapshot、time_span、scene、event。"""
    path = [harness.read(original)]
    for _ in range(3):
        child = path[-1]
        path.append(harness.composition.read(
            child.hierarchy.resolved_parent_scope(child.scope), child.hierarchy.parent_id,
        ))
    return path
