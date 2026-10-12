# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""限制显式建树的类型与读取边界，API、Engine 和 Job 共用。"""

from datetime import datetime, timezone

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import HierarchyKind, HierarchyRole, Scope
from jiuwen_memory.construction.evolver import EvolveMode
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposeOptions,
    validate_time_parent_roles,
)
from jiuwen_memory.control.types import Channel, EvolveTaskOptions


def _validate_scope(scope: Scope) -> None:
    if not isinstance(scope, Scope):
        raise ValidationError("建树 scope 必须是 Scope")
    for dimension in ("org", "space", "user", "agent", "session"):
        if not isinstance(getattr(scope, dimension), str):
            raise ValidationError("Scope 的五维字段必须是字符串")


def scope_contains(home: Scope, candidate: Scope) -> bool:
    """org/space 精确相同；其余维度仅在 home 非空时限定。"""
    _validate_scope(home)
    _validate_scope(candidate)
    if home.org != candidate.org or home.space != candidate.space:
        return False
    for dimension in ("user", "agent", "session"):
        expected = getattr(home, dimension)
        if expected and expected != getattr(candidate, dimension):
            return False
    return True


def validate_hierarchy_options(scope: Scope, options: HierarchyComposeOptions) -> None:
    """要求显式 TIME 两/三/四层、有界区间，且任务与父驻留 Scope 完全一致。"""
    _validate_scope(scope)
    if not isinstance(options, HierarchyComposeOptions):
        raise ValidationError("HIERARCHY 要求 HierarchyComposeOptions")
    _validate_scope(options.tree_home_scope)
    if scope != options.tree_home_scope:
        raise ValidationError("scope 必须等于 tree_home_scope，不得扩大建树范围")
    if options.kind is not HierarchyKind.TIME or options.leaf_role is not HierarchyRole.SNAPSHOT:
        raise ValidationError("显式建树当前只支持 TIME snapshot → time_span → scene → event")
    validate_time_parent_roles(options.parent_roles)
    if not isinstance(options.span_start, datetime) or not isinstance(options.span_end, datetime):
        raise ValidationError("显式建树要求成对有界的 datetime span")
    start = options.span_start
    end = options.span_end
    start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    if start > end:
        raise ValidationError("span_start 不得晚于 span_end")
    if not isinstance(options.metadata, dict):
        raise ValidationError("hierarchy metadata 必须是 dict[str, str]")
    for metadata_key, metadata_value in options.metadata.items():
        if not isinstance(metadata_key, str) or not isinstance(metadata_value, str):
            raise ValidationError("hierarchy metadata 必须是 dict[str, str]")


def validate_evolve_options(scope: Scope, options: EvolveTaskOptions) -> None:
    """校验公开请求对象，禁止在普通模式携带建树参数。"""
    if not isinstance(options, EvolveTaskOptions):
        raise ValidationError("evolve 要求 EvolveTaskOptions，不再接受独立 mode/channel")
    if not isinstance(options.mode, EvolveMode) or not isinstance(options.channel, Channel):
        raise ValidationError("EvolveTaskOptions 要求合法的 EvolveMode 和 Channel")
    if options.mode is EvolveMode.HIERARCHY:
        validate_hierarchy_options(scope, options.hierarchy_options)
    elif options.hierarchy_options is not None:
        raise ValidationError("仅 HIERARCHY 接受 hierarchy_options")
