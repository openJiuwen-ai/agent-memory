# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MemoryUnit 的公共 FilterExpr 求值语义。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from jiuwen_memory.common.time_utils import epoch_ms

from .filter import FilterClause, FilterExpr, FilterOp, evaluate, filter_field_metadata_key
from .hierarchy import HIERARCHY_INDEX_KEYS, hierarchy_index_metadata
from .memory import T_EVENT_UNKNOWN, T_INVALID_OPEN, MemoryUnit

COLLECTIVE_DOCUMENT_ROOT = "memory/team_memory"
_COLLECTIVE_PATH_DESTINATIONS = {
    f"{COLLECTIVE_DOCUMENT_ROOT}/projects/": "project",
    f"{COLLECTIVE_DOCUMENT_ROOT}/teams/": "team",
    f"{COLLECTIVE_DOCUMENT_ROOT}/groups/": "group",
    f"{COLLECTIVE_DOCUMENT_ROOT}/agents/": "agent",
}
COLLECTIVE_DOCUMENT_PREFIXES = tuple(_COLLECTIVE_PATH_DESTINATIONS)
IS_CREDENTIAL_FILTER_FIELD = "system_metadata.is_credential"


def normalize_is_credential_value(value: object) -> str | None:
    """将 is_credential 投影值归一为 'true' 或 None（兼容 bool True 与不区分大小写 'true'）。"""
    if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
        return "true"
    return None


def normalize_is_credential_target(value: object) -> object:
    """仅将凭据真值比较目标归一为 'true'，非真值目标保持原值以避免误匹配 None。"""
    return "true" if normalize_is_credential_value(value) == "true" else value


def normalize_is_credential_clause_value(
    field: str, op: FilterOp, value: object
) -> object:
    """对 system_metadata.is_credential 谓词比较目标做真值归一（与真源求值同口径）。

    索引侧仅存在 'true' 或缺失/NULL：对于 IN/NOT_IN，只要目标集含任一真值即归约为
    同构 ['true']（防 [True, False] 变成异构 ['true', False] 破坏 Milvus/ES）；
    不含真值时原样保留同类型列表，避免空数组语法错误。
    """
    if field != IS_CREDENTIAL_FILTER_FIELD:
        return value
    if op in (FilterOp.IN, FilterOp.NOT_IN) and isinstance(value, (list, tuple, set)):
        if any(normalize_is_credential_value(item) == "true" for item in value):
            return ["true"]
        return list(value)
    return normalize_is_credential_target(value)


def collective_destination_of(metadata: Mapping[str, object]) -> str:
    destination = metadata.get("collective_destination")
    if isinstance(destination, str) and destination:
        return destination
    path = str(metadata.get("md_filename", "")).replace("\\", "/")
    return next((kind for prefix, kind in _COLLECTIVE_PATH_DESTINATIONS.items()
                 if path.startswith(prefix)), "")


def is_collective_metadata(metadata: Mapping[str, object]) -> bool:
    return bool(collective_destination_of(metadata))


def _epoch_ms(value: datetime | None) -> int | None:
    return None if value is None else epoch_ms(value)


def _field_value(unit: MemoryUnit, field: str):
    if field == "system_metadata.collective_destination":
        value = unit.system_metadata.get("collective_destination")
        return "" if value is None else value
    if field in {
        "system_metadata.collective.project", "system_metadata.collective.team",
        "system_metadata.collective.group", "system_metadata.collective.shareable",
    }:
        name = field.rsplit(".", 1)[1]
        metadata = unit.system_metadata
        if name == "shareable":
            value = metadata.get(name)
            if isinstance(value, bool):
                return value
            return not is_collective_metadata(metadata)
        return metadata.get(name) or ""
    if field in HIERARCHY_INDEX_KEYS:
        # 裸结构字段读真源；显式 metadata 路径仍保持独立命名空间。
        try:
            return hierarchy_index_metadata(unit.hierarchy).get(field)
        except (AttributeError, TypeError, ValueError):
            return None
    if field == "tags":
        return unit.tags
    if field == "tier":
        return unit.tier.value
    if field == "source":
        return unit.source.value
    if field == "lifecycle":
        return unit.lifecycle.value
    if field in ("unit_id", "id"):
        return unit.id
    if field == "t_event":
        # 与索引投影对称：真源 None → 哨兵 T_EVENT_UNKNOWN，使事件窗 OR 组的 EQ 0
        # 分支在后置复核里同样成立（候选不被 is_retrieval_candidate 误砍）。
        # in_event_window 仍读真源 datetime、对 None/naive 放行，与此不冲突。
        value = _epoch_ms(unit.temporal.t_event)
        return T_EVENT_UNKNOWN if value is None else value
    if field == "t_valid":
        return _epoch_ms(unit.temporal.t_valid)
    if field == "t_invalid":
        value = _epoch_ms(unit.temporal.t_invalid)
        return T_INVALID_OPEN if value is None else value
    if field == "t_message":
        return _epoch_ms(unit.temporal.t_message)
    if field == "t_ingest":
        # 恒非空（内核接入路径强制盖章，dreaming F13 D5）；防御性对齐 t_message 的
        # None 语义（None 只匹配 NE/NOT_IN），恒写投影下不会有实际 None。
        return _epoch_ms(unit.temporal.t_ingest)
    if field == IS_CREDENTIAL_FILTER_FIELD:
        return normalize_is_credential_value(unit.system_metadata.get("is_credential"))
    key = filter_field_metadata_key(field)
    if key.startswith("user_metadata."):
        return unit.user_metadata.get(key.removeprefix("user_metadata."))
    if key.startswith("system_metadata."):
        return unit.system_metadata.get(key.removeprefix("system_metadata."))
    return None


def memory_field_value(unit: MemoryUnit, field: str):
    """读取 ``field`` 在真源上的投影值（与过滤求值同源）。

    F09 分片规则需要「取字段值」而非「判断是否匹配」，两者必须共用同一套
    投影语义（枚举取 ``value``、嵌套 metadata 走扁平路径、缺值返回 ``None``），
    否则规则与 filters 会在同一字段上给出不同答案。
    """
    return _field_value(unit, field)


def _matches_clause(unit: MemoryUnit, clause: FilterClause) -> bool:
    return matches_filter_value(_field_value(unit, clause.field), clause)


def matches_filter_value(value: object, clause: FilterClause) -> bool:
    """真源与内存索引共用标量、数组和缺值的过滤语义。"""
    op, target = clause.op, clause.value
    if clause.field == IS_CREDENTIAL_FILTER_FIELD:
        value = normalize_is_credential_value(value)
        target = normalize_is_credential_clause_value(clause.field, op, target)

    if isinstance(value, (list, tuple, set)):  # 集合字段（如 tags）
        members = set(value)
        # 形态语义严格分离：CONTAINS 只命中数组成员，EQ / IN 只命中标量，
        # 两者不重叠；NE / NOT_IN 分别是后两者的逻辑否定，对数组恒真。
        if op is FilterOp.CONTAINS:
            return target in members
        if op is FilterOp.EQ:
            return False
        if op is FilterOp.NE:
            return True
        if op is FilterOp.IN:
            return False
        if op is FilterOp.NOT_IN:
            return True
        # 范围算子对集合无意义：判否，与下方标量不可比时的 TypeError 分支同答案。
        # 放行会让该谓词失效——真源复核是正确性边界，不可判定的组合不能当作不约束。
        return False

    if value is None:
        return op in (FilterOp.NE, FilterOp.NOT_IN)
    if op is FilterOp.EQ:
        return value == target
    if op is FilterOp.NE:
        return value != target
    if op is FilterOp.IN:
        return value in (target or [])
    if op is FilterOp.NOT_IN:
        return value not in (target or [])
    if op is FilterOp.CONTAINS:
        return False  # 标量既不做等值退化，也不做字符串子串匹配
    try:
        if op is FilterOp.GT:
            return value > target
        if op is FilterOp.GTE:
            return value >= target
        if op is FilterOp.LT:
            return value < target
        if op is FilterOp.LTE:
            return value <= target
    except TypeError:
        return False
    return True


def matches_memory_unit(unit: MemoryUnit, filters: FilterExpr | None) -> bool:
    """按统一字段投影和比较规则判断 MemoryUnit 是否满足过滤表达式。"""
    return evaluate(filters, lambda clause: _matches_clause(unit, clause))
