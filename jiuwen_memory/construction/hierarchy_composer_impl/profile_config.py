# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配期解析 TIME 两/三层树 profile，拒绝未实现的树形及配置。

hierarchy_profiles.time 可声明 leaf_role=snapshot、parent_roles=[time_span]、
stage_options.TimeSpanMerger/SceneSegmenter；模型依赖由 Composer 装配时校验。
"""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposeProfile

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
    if tuple(declared_roles) not in (("time_span",), ("time_span", "scene")):
        raise ValidationError("parent_roles 只支持 [time_span] 或 [time_span, scene]")
    stage_options = _stage_options(spec.get("stage_options"))
    if "SceneSegmenter" in stage_options and "scene" not in declared_roles:
        raise ValidationError("SceneSegmenter 要求 parent_roles 包含 scene")
    TimeSpanMergerOptions.from_stage_options(stage_options.get("TimeSpanMerger", {}))
    SceneSegmenterOptions.from_stage_options(stage_options.get("SceneSegmenter", {}))
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
    if set(raw) - {"TimeSpanMerger", "SceneSegmenter"}:
        raise ValidationError("stage_options 只支持 TimeSpanMerger/SceneSegmenter")
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
