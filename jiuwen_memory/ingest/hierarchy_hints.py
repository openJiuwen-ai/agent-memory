# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""接入层叶提示：``RawPayload.system_metadata`` 的 ``hierarchy_*`` 保留键 → ``HierarchyRef``。

来源适配器只能声明"我是哪种树的一片叶、覆盖哪段时间"，**不能声明父子边**——边只由
构建层在持久化阶段校验并维护。因此本模块只产出叶安全字段（``kind``/``role``/
``span_start``/``span_end``），``parent_id``/``child_ids`` 恒为空、``status`` 恒为 ACTIVE。

提示是"来源对自身结构身份的声明"，不证明边存在。任一提示无效即拒绝整条 payload 的
转换，不产出半有效结构；非 ``hierarchy_`` 前缀的 metadata 原样透传。
未提供提示时返回空结构与系统元数据副本；非法提示抛 ValidationError，由调用方拒绝
该 payload。朴素时间在比较时按 UTC 解释，返回值仍保留输入的时间表示。
"""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    LEAF_ROLE_BY_KIND,
    HierarchyKind,
    HierarchyRef,
    HierarchyRole,
    HierarchyStatus,
    MetadataValueType,
)

HINT_PREFIX = "hierarchy_"

HINT_KIND = "hierarchy_kind"
HINT_ROLE = "hierarchy_role"
HINT_SPAN_START = "hierarchy_span_start"
HINT_SPAN_END = "hierarchy_span_end"

# 接入层允许出现的全部保留键。其余 hierarchy_ 前缀键（hierarchy_parent_id、
# hierarchy_child_ids 等试图建边的声明）一律拒绝，不作为普通 metadata 静默保留。
ALLOWED_HINTS = frozenset({HINT_KIND, HINT_ROLE, HINT_SPAN_START, HINT_SPAN_END})


def extract_hierarchy_hints(
    system_metadata: dict[str, MetadataValueType],
) -> tuple[HierarchyRef, dict[str, MetadataValueType]]:
    """校验叶提示，返回 HierarchyRef 与移除提示后的系统元数据副本。"""
    reserved = {k: v for k, v in system_metadata.items() if k.startswith(HINT_PREFIX)}
    rest = {k: v for k, v in system_metadata.items() if not k.startswith(HINT_PREFIX)}

    # 规则 5：试图建边或未定义的保留前缀键，一律拒绝
    unknown = sorted(set(reserved) - ALLOWED_HINTS)
    if unknown:
        raise ValidationError(
            f"接入层不接受的 hierarchy 保留键：{unknown}（父子边只能由构建层建立）"
        )

    if not reserved:
        return HierarchyRef(), rest

    raw_kind = reserved.get(HINT_KIND)
    raw_role = reserved.get(HINT_ROLE)
    raw_start = reserved.get(HINT_SPAN_START)
    raw_end = reserved.get(HINT_SPAN_END)

    # 规则 1：kind/role 同在；区间同在或同缺
    if (raw_kind is None) != (raw_role is None):
        raise ValidationError(f"{HINT_KIND} 与 {HINT_ROLE} 必须同时提供")
    if raw_kind is None:
        raise ValidationError(f"提供了区间提示却缺少 {HINT_KIND}/{HINT_ROLE}")
    if (raw_start is None) != (raw_end is None):
        raise ValidationError(f"{HINT_SPAN_START} 与 {HINT_SPAN_END} 必须同时提供")

    # 规则 2：枚举精确匹配
    kind = _parse_enum(HierarchyKind, raw_kind, HINT_KIND)
    role = _parse_enum(HierarchyRole, raw_role, HINT_ROLE)

    # 规则 4：接入只接受该 kind 的叶角色，父侧角色必须由构建层创建
    leaf_role = LEAF_ROLE_BY_KIND[kind]
    if role is not leaf_role:
        raise ValidationError(
            f"{HINT_ROLE} 只接受 {kind.value} 的叶角色 {leaf_role.value}，"
            f"实际 {role.value}——父侧角色必须由构建层创建"
        )

    # 规则 3：TIME 必须提供区间
    if raw_start is None:
        if kind is HierarchyKind.TIME:
            raise ValidationError(f"{HierarchyKind.TIME.value} 必须提供区间提示")
        return HierarchyRef(kind=kind, role=role, status=HierarchyStatus.ACTIVE), rest

    # 规则 2：时间可解析且起点不晚于终点
    span_start = _parse_time(raw_start, HINT_SPAN_START)
    span_end = _parse_time(raw_end, HINT_SPAN_END)
    # 与索引口径一致：朴素时间按 UTC 比较，但保留输入的原时间表示与微秒精度。
    comparable_start = span_start if span_start.tzinfo else span_start.replace(tzinfo=timezone.utc)
    comparable_end = span_end if span_end.tzinfo else span_end.replace(tzinfo=timezone.utc)
    if comparable_start > comparable_end:
        raise ValidationError(f"{HINT_SPAN_START} 不得晚于 {HINT_SPAN_END}")

    return (
        HierarchyRef(
            kind=kind,
            role=role,
            span_start=span_start,
            span_end=span_end,
            status=HierarchyStatus.ACTIVE,
        ),
        rest,
    )


def _parse_enum(enum_cls, value: object, key: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = sorted(member.value for member in enum_cls)
        raise ValidationError(f"{key} 取值非法：{value!r}，允许 {allowed}") from exc


def _parse_time(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{key} 必须是 ISO 8601 字符串，实际 {type(value).__name__}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{key} 不是合法的 ISO 8601 时间：{value!r}") from exc
