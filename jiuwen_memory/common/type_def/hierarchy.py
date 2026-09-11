# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""跨 ``MemoryUnit`` 的树结构引用与无副作用校验（F08 树结构轴）。

树结构轴与既有三轴正交，任一轴不得推导另一轴：``ContentLayers``/``DisclosureLevel``
是同一 unit 的 L0/L1/L2 披露；``system_metadata.memory_level`` + ``provenance`` 是单媒体源的
多模态构建粒度；``MemoryTier`` 是认知角色；本模块的 :class:`HierarchyRef` 才表达
**跨 unit 的父子包含**。

本模块只放结构定义与纯函数校验——无副作用、不依赖存储后端，第一阶段提供校验工具，
后续 construction 在保存候选子树前接入；ingest 使用独立的叶提示校验。

字段校验要求 kind/role 同空同在、空结构不带边或区间、区间成对且有序，TIME 必须有区间。
子引用按完整 Scope + id 去重；未声明显式 Scope 的引用与 owner 同 Scope，可直接判断
自指。显式 Scope 与 owner 的身份比较留给 ``validate_tree``，不把跨 Scope 同名 ID 误判。

树校验先检查全部输入的字段，再忽略合法空结构；节点和已声明的引用 Scope 都受 org+space
硬边界约束，跨非空 user/agent 须显式放开，session 可跨。集合内边核对双向完整身份、
无环、单父及父区间覆盖；集合外只跳过依赖邻居内容的核对，不跳过已知引用的身份与边界。
所有区间比较把朴素时间按 UTC 解读，保留 datetime 微秒精度，不经索引的毫秒投影比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

from ..errors import ValidationError
from .scope import Scope

if TYPE_CHECKING:  # 仅用于注解：运行时只读 .id/.scope/.hierarchy，避免与 memory 循环导入
    from .memory import MemoryUnit


class HierarchyKind(str, Enum):
    """结构维度：一次遍历只处理一个 kind，不隐式跨 kind。"""

    TIME = "time"
    TOPIC = "topic"
    DIRECTORY = "directory"
    CLUSTER = "cluster"
    CUSTOM = "custom"


class HierarchyRole(str, Enum):
    """树位角色——表示结构位置，不等于 ``MemoryTier`` 的认知角色。

    ``PROFILE`` 是画像组织角色：产出独立 unit（常 ``CORE``），**不**通过
    ``parent_id``/``child_ids`` 与 TIME 主树节点互挂。
    """

    SNAPSHOT = "snapshot"
    TIME_SPAN = "time_span"
    SCENE = "scene"
    EVENT = "event"
    PROFILE = "profile"
    ROOT = "root"
    NODE = "node"


class HierarchyStatus(str, Enum):
    """结构修正状态——只表示节点在树里是否算数。

    归档、遗忘与版本失效一律由 ``LifecycleState`` 表达，不进入本枚举。
    """

    ACTIVE = "active"
    DISMISSED = "dismissed"


# 结构索引副本的完整键集合；所有索引入口用同一集合清理陈旧投影。
HIERARCHY_INDEX_KEYS = frozenset(
    {"hierarchy_kind", "hierarchy_role", "hierarchy_status", "parent_id", "span_start", "span_end"}
)


# 接入层只允许声明叶角色，父侧角色必须由构建层创建（F08 接入层校验规则 4）。
LEAF_ROLE_BY_KIND: dict[HierarchyKind, HierarchyRole] = {
    HierarchyKind.TIME: HierarchyRole.SNAPSHOT,
    HierarchyKind.TOPIC: HierarchyRole.NODE,
    HierarchyKind.DIRECTORY: HierarchyRole.NODE,
    HierarchyKind.CLUSTER: HierarchyRole.NODE,
    HierarchyKind.CUSTOM: HierarchyRole.NODE,
}


@dataclass
class HierarchyRef:
    """内嵌于 ``MemoryUnit`` 的结构引用：树位 + 直接双向边 + 覆盖区间 + 稳定顺序。

    ``child_scopes`` 与 ``child_ids`` 等长或为空；为空表示全部子节点与本 unit 的完整
    Scope 相同。``parent_scope`` 为 ``None`` 表示父与本 unit 完整 Scope 相同。因 unit id
    只在完整 Scope 内唯一，跨细粒度 scope 的边必须携带这两个字段才能唯一定位。
    """

    kind: HierarchyKind | None = None
    role: HierarchyRole | None = None
    parent_id: str = ""  # 空表示根或尚未挂接
    child_ids: list[str] = field(default_factory=list)  # 直接子的稳定有序列表
    child_scopes: list[Scope] = field(default_factory=list)  # 与 child_ids 等长或为空
    parent_scope: Scope | None = None  # None 表示与本 unit 同 Scope
    span_start: datetime | None = None  # 结构覆盖区间起点（TIME 必填）
    span_end: datetime | None = None  # 结构覆盖区间终点（TIME 必填）
    ordinal: int = 0  # 同父下的排序提示（非 TIME kind 用）
    status: HierarchyStatus = HierarchyStatus.ACTIVE

    @property
    def is_empty(self) -> bool:
        """空结构：``kind`` 与 ``role`` 同时缺省，等价于未启用树结构。"""
        return self.kind is None and self.role is None

    def child_scope_at(self, index: int, owner_scope: Scope) -> Scope:
        """第 ``index`` 个子节点的驻留 Scope；``child_scopes`` 为空时退化为 owner 的 Scope。"""
        if not self.child_scopes:
            return owner_scope
        return self.child_scopes[index]

    def resolved_parent_scope(self, owner_scope: Scope) -> Scope:
        """父节点的驻留 Scope；``parent_scope`` 为空时退化为 owner 的 Scope。"""
        return self.parent_scope if self.parent_scope is not None else owner_scope


def hierarchy_index_metadata(ref: HierarchyRef) -> dict[str, object]:
    """结构过滤所需的索引投影（S06 层级过滤契约）——空结构返回空字典。

    六个键在此单点定义，全文与向量两侧各自调用，避免两处投影漂移；同一 unit 的
    L0/L1/L2 记录必须携带完全相同的结果。

    区间写 **epoch 毫秒（UTC）**，与 ``t_event`` / ``t_valid`` / ``t_invalid`` 同口径：
    ``FilterClause`` 的范围算子只接受有限数值（见 ``filter.py``），ISO 字符串做
    ``LTE``/``GTE`` 会被校验拒绝，各召回路静默降级为空。数值同时免去了"同一索引内
    必须规范到统一时区格式"的负担。

    ``hierarchy_status`` 在非空结构下恒写——缺字段会被后端按"缺失即不匹配"排他，
    等到有 ``DISMISSED`` 时再补会让存量节点整体从层级召回中消失（同 ``t_event``
    /``t_invalid`` 恒写哨兵的既有教训）。

    **区间缺省时不落哨兵**：没有 span 的节点本就不该匹配有 span 的查询（S04），
    与 ``t_event`` 的"未知时间 ≠ 窗外"取舍相反，两者不可照抄。
    """
    if ref.is_empty:
        return {}
    metadata: dict[str, object] = {
        "hierarchy_kind": ref.kind.value if ref.kind is not None else "",
        "hierarchy_role": ref.role.value if ref.role is not None else "",
        "hierarchy_status": ref.status.value,
        "parent_id": ref.parent_id,
    }
    if ref.span_start is not None and ref.span_end is not None:
        metadata["span_start"] = span_epoch_ms(ref.span_start)
        metadata["span_end"] = span_epoch_ms(ref.span_end)
    return metadata


def span_epoch_ms(value: datetime) -> int:
    """区间时间 → epoch 毫秒（UTC）；朴素时间按 UTC 解读。

    索引投影与检索谓词共用本函数，两侧口径不会漂移。
    """
    return int(_as_utc(value).timestamp() * 1000)


def validate_ref(ref: HierarchyRef, *, unit_id: str = "") -> None:
    """校验单节点字段及无需 owner Scope 即可确定的重复引用和自指。"""
    where = f"（unit={unit_id}）" if unit_id else ""
    if (ref.kind is None) != (ref.role is None):
        raise ValidationError(f"hierarchy kind 与 role 必须同时设置或同时缺省{where}")
    if ref.is_empty:
        if ref.parent_id or ref.child_ids:
            raise ValidationError(f"空 hierarchy 不得携带父子引用{where}")
        if ref.child_scopes or ref.parent_scope is not None:
            raise ValidationError(f"空 hierarchy 不得携带父子引用{where}")
        if ref.span_start is not None or ref.span_end is not None:
            raise ValidationError(f"空 hierarchy 不得携带区间{where}")
        return
    if (ref.span_start is None) != (ref.span_end is None):
        raise ValidationError(f"hierarchy 区间必须成对出现{where}")
    if ref.span_start is not None and ref.span_end is not None:
        if _as_utc(ref.span_start) > _as_utc(ref.span_end):
            raise ValidationError(f"hierarchy span_start 不得晚于 span_end{where}")
    elif ref.kind is HierarchyKind.TIME:
        raise ValidationError(f"HierarchyKind.TIME 的所有节点必须有区间{where}")
    if ref.child_scopes and len(ref.child_scopes) != len(ref.child_ids):
        raise ValidationError(f"child_scopes 非空时必须与 child_ids 等长{where}")
    if ref.child_scopes:
        child_keys = {
            _key(child_scope, child_id)
            for child_scope, child_id in zip(ref.child_scopes, ref.child_ids)
        }
        repeated_children = len(child_keys) != len(ref.child_ids)
    else:
        repeated_children = len(set(ref.child_ids)) != len(ref.child_ids)
    if repeated_children:
        raise ValidationError(f"child_ids 不得重复{where}")
    if unit_id and ref.parent_scope is None and ref.parent_id == unit_id:
        raise ValidationError(f"hierarchy parent_id 不得自指{where}")
    if unit_id and not ref.child_scopes and unit_id in ref.child_ids:
        raise ValidationError(f"hierarchy child_ids 不得包含自身{where}")


def validate_tree(
    units: list[MemoryUnit],
    *,
    allow_cross_user: bool = False,
) -> None:
    """提交前校验候选子树的字段、完整节点身份、边界和集合内关系，不读写存储。"""
    for unit in units:
        validate_ref(unit.hierarchy, unit_id=unit.id)
    nodes = [u for u in units if not u.hierarchy.is_empty]
    if not nodes:
        return

    kinds = {u.hierarchy.kind for u in nodes}
    if len(kinds) > 1:
        raise ValidationError(f"候选子树必须同 kind，实际含 {sorted(k.value for k in kinds)}")

    _validate_scope_boundary(nodes, allow_cross_user=allow_cross_user)
    index = _index_by_key(nodes)
    _validate_self_references(nodes)
    # 单父先于双向：多父是更根本的结构缺陷，先报它比报"双向不一致"更贴近成因。
    _validate_single_parent(nodes)
    _validate_bidirectional(nodes, index)
    _validate_acyclic(nodes, index)
    _validate_span_coverage(nodes, index)


# -- 内部实现 --------------------------------------------------------------- #

class _NodeKey(NamedTuple):
    org: str
    space: str
    user: str
    agent: str
    session: str
    unit_id: str


def _as_utc(value: datetime) -> datetime:
    """返回用于比较的 UTC 时间，朴素时间按 UTC 解读且不截断微秒。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _key(scope: Scope, unit_id: str) -> _NodeKey:
    return _NodeKey(scope.org, scope.space, scope.user, scope.agent, scope.session, unit_id)


def _index_by_key(nodes: list[MemoryUnit]) -> dict[_NodeKey, MemoryUnit]:
    index: dict[_NodeKey, MemoryUnit] = {}
    for unit in nodes:
        node_key = _key(unit.scope, unit.id)
        if node_key in index:
            raise ValidationError(f"候选子树含重复节点：scope={unit.scope} id={unit.id}")
        index[node_key] = unit
    return index


def _validate_self_references(nodes: list[MemoryUnit]) -> None:
    """带 owner Scope 后按完整身份判断自指，跨 Scope 同名 ID 不算自身。"""
    for unit in nodes:
        own_key = _key(unit.scope, unit.id)
        ref = unit.hierarchy
        if ref.parent_id and _key(ref.resolved_parent_scope(unit.scope), ref.parent_id) == own_key:
            raise ValidationError(f"hierarchy parent_id 不得自指（unit={unit.id}）")
        for position, child_id in enumerate(ref.child_ids):
            if _key(ref.child_scope_at(position, unit.scope), child_id) == own_key:
                raise ValidationError(f"hierarchy child_ids 不得包含自身（unit={unit.id}）")


def _validate_scope_boundary(nodes: list[MemoryUnit], *, allow_cross_user: bool) -> None:
    """org+space 是硬边界；user/agent 默认不可跨；session 默认可跨。"""
    scopes: list[Scope] = []
    for unit in nodes:
        scopes.append(unit.scope)
        scopes.extend(unit.hierarchy.child_scopes)
        if unit.hierarchy.parent_id and unit.hierarchy.parent_scope is not None:
            scopes.append(unit.hierarchy.parent_scope)
    first = scopes[0]
    for declared_scope in scopes:
        if declared_scope.org != first.org or declared_scope.space != first.space:
            raise ValidationError(
                f"结构边不得跨 org/space：{first.org}/{first.space} vs "
                f"{declared_scope.org}/{declared_scope.space}"
            )
    if allow_cross_user:
        return
    # 按维度各自比较**非空值**：父常写在清空 user/agent 的 tree home scope，空值只表示
    # 归属层级更粗（父覆盖子），不是"另一个主体"，故不计入跨主体判定。
    for dimension in ("user", "agent"):
        named = {getattr(item, dimension) for item in scopes if getattr(item, dimension)}
        if len(named) > 1:
            raise ValidationError(f"未开启跨 {dimension} 建树，实际含 {sorted(named)}")


def _validate_bidirectional(nodes: list[MemoryUnit], index: dict[_NodeKey, MemoryUnit]) -> None:
    """对集合内的边核对双向一致：父列子 ⟺ 子认父，且 scope 定位互相对得上。"""
    for parent in nodes:
        ref = parent.hierarchy
        for position, child_id in enumerate(ref.child_ids):
            child_scope = ref.child_scope_at(position, parent.scope)
            child = index.get(_key(child_scope, child_id))
            if child is None:
                continue  # 集合外引用，无从核对
            if child.hierarchy.parent_id != parent.id:
                raise ValidationError(
                    f"双向边不一致：父 {parent.id} 列有子 {child_id}，"
                    f"但该子 parent_id={child.hierarchy.parent_id!r}"
                )
            resolved = child.hierarchy.resolved_parent_scope(child.scope)
            if _key(resolved, parent.id) != _key(parent.scope, parent.id):
                raise ValidationError(
                    f"子 {child_id} 的 parent_scope 与父 {parent.id} 的驻留 Scope 不一致"
                )
    for child in nodes:
        parent_id = child.hierarchy.parent_id
        if not parent_id:
            continue
        parent_scope = child.hierarchy.resolved_parent_scope(child.scope)
        parent = index.get(_key(parent_scope, parent_id))
        if parent is None:
            continue  # 集合外引用，无从核对
        parent_ref = parent.hierarchy
        child_keys = {
            _key(parent_ref.child_scope_at(listed_position, parent.scope), listed_child_id)
            for listed_position, listed_child_id in enumerate(parent_ref.child_ids)
        }
        if _key(child.scope, child.id) not in child_keys:
            raise ValidationError(
                f"双向边不一致：子 {child.id} 认父 {parent_id}，但该父 child_ids 未列出它"
            )


def _validate_single_parent(nodes: list[MemoryUnit]) -> None:
    """同一 kind 下每个节点最多一个父——查是否被多个父同时列入 child_ids。"""
    claimed: dict[_NodeKey, _NodeKey] = {}
    for parent in nodes:
        ref = parent.hierarchy
        parent_key = _key(parent.scope, parent.id)
        for position, child_id in enumerate(ref.child_ids):
            child_key = _key(ref.child_scope_at(position, parent.scope), child_id)
            previous = claimed.get(child_key)
            if previous is not None and previous != parent_key:
                raise ValidationError(
                    f"单 kind 多父：子 {child_id} 同时被 {previous} 与 {parent_key} 列为子节点"
                )
            claimed[child_key] = parent_key


def _validate_acyclic(nodes: list[MemoryUnit], index: dict[_NodeKey, MemoryUnit]) -> None:
    """沿 parent_id 上溯探环——单父树下上溯路径唯一，命中已访问节点即成环。"""
    for start in nodes:
        seen: set[_NodeKey] = {_key(start.scope, start.id)}
        cursor = start
        while cursor.hierarchy.parent_id:
            parent_scope = cursor.hierarchy.resolved_parent_scope(cursor.scope)
            parent_key = _key(parent_scope, cursor.hierarchy.parent_id)
            if parent_key in seen:
                raise ValidationError(f"hierarchy 成环：起点 {start.id} 上溯回到已访问节点")
            parent = index.get(parent_key)
            if parent is None:
                break  # 集合外引用，无从继续
            seen.add(parent_key)
            cursor = parent


def _validate_span_coverage(nodes: list[MemoryUnit], index: dict[_NodeKey, MemoryUnit]) -> None:
    """父区间必须覆盖每个直接子区间，不得缩小到遗漏直接子。"""
    for parent in nodes:
        ref = parent.hierarchy
        if ref.span_start is None or ref.span_end is None:
            continue
        parent_start, parent_end = _as_utc(ref.span_start), _as_utc(ref.span_end)
        for position, child_id in enumerate(ref.child_ids):
            child = index.get(_key(ref.child_scope_at(position, parent.scope), child_id))
            if child is None:
                continue
            child_ref = child.hierarchy
            if child_ref.span_start is None or child_ref.span_end is None:
                continue
            if (
                _as_utc(child_ref.span_start) < parent_start
                or _as_utc(child_ref.span_end) > parent_end
            ):
                raise ValidationError(f"父 {parent.id} 的区间未覆盖子 {child_id} 的区间")
