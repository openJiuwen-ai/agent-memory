# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配期解析 TIME 两/三/四层树 profile，拒绝未实现的树形及配置。

hierarchy_profiles.time 可声明 leaf_role=snapshot、parent_roles=[time_span]、
stage_options.TimeSpanMerger/SceneSegmenter/EventBuilder；模型依赖由 Composer 装配时校验。
"""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer import TIME_PARENT_ROLES, HierarchyComposeProfile

from .event_pipeline import EventBuilderOptions
from .scene_pipeline import SceneSegmenterOptions
from .time_pipeline import TimeSpanMergerOptions


def build_profiles(raw: object) -> dict[HierarchyKind, HierarchyComposeProfile]:
    """解析 hierarchy_profiles；未配置返回空字典，建树采用算法默认值。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationError("hierarchy_profiles 必须是 kind → profile 的映射")
    profiles: dict[HierarchyKind, HierarchyComposeProfile] = {}
    for kind_name, spec in raw.items():
        if kind_name != HierarchyKind.TIME.value:
            raise ValidationError("hierarchy_profiles 当前只支持 time")
        profiles[HierarchyKind.TIME] = _profile(spec)
    return profiles


def _profile(spec: object) -> HierarchyComposeProfile:
    if not isinstance(spec, Mapping):
        raise ValidationError("hierarchy_profiles.time 必须是映射")
    unknown = set(spec) - {"leaf_role", "parent_roles", "stage_options"}
    if unknown:
        raise ValidationError(f"hierarchy_profiles.time 含未支持字段：{unknown}")
    if spec.get("leaf_role", "snapshot") != "snapshot":
        raise ValidationError("hierarchy_profiles.time.leaf_role 只能是 snapshot")
    declared_roles = spec.get("parent_roles")
    if isinstance(declared_roles, str):
        declared_roles = [item.strip() for item in declared_roles.split(",")]
    if not isinstance(declared_roles, (list, tuple)):
        raise ValidationError("hierarchy_profiles.time.parent_roles 必须配置为 [time_span]")
    if not 1 <= len(declared_roles) <= len(TIME_PARENT_ROLES) or (
        tuple(declared_roles) != TIME_PARENT_ROLES[:len(declared_roles)]
    ):
        raise ValidationError("parent_roles 只支持 [time_span, scene, event] 的非空前缀")
    stage_options = _stage_options(spec.get("stage_options"))
    if "SceneSegmenter" in stage_options and "scene" not in declared_roles:
        raise ValidationError("SceneSegmenter 要求 parent_roles 包含 scene")
    if "EventBuilder" in stage_options and "event" not in declared_roles:
        raise ValidationError("EventBuilder 要求 parent_roles 包含 event")
    TimeSpanMergerOptions.from_stage_options(stage_options.get("TimeSpanMerger", {}))
    SceneSegmenterOptions.from_stage_options(stage_options.get("SceneSegmenter", {}))
    EventBuilderOptions.from_stage_options(stage_options.get("EventBuilder", {}))
    return HierarchyComposeProfile(
        kind=HierarchyKind.TIME,
        leaf_role=HierarchyRole.SNAPSHOT,
        parent_roles=tuple(HierarchyRole(role) for role in declared_roles),
        stage_options=stage_options,
    )


def _stage_options(raw: object) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationError("stage_options 必须是映射")
    if set(raw) - {"TimeSpanMerger", "SceneSegmenter", "EventBuilder"}:
        raise ValidationError("stage_options 只支持 TimeSpanMerger/SceneSegmenter/EventBuilder")
    options: dict[str, dict[str, str]] = {}
    for stage_name, stage_values in raw.items():
        if not isinstance(stage_values, Mapping):
            raise ValidationError(f"stage_options.{stage_name} 必须是映射")
        values: dict[str, str] = {}
        for option_key, option_value in stage_values.items():
            if not isinstance(option_key, str) or option_value is None:
                raise ValidationError("阶段配置键必须为字符串，值不得为 None")
            values[option_key] = str(option_value)
        options[stage_name] = values
    return options
