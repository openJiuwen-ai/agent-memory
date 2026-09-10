# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""显式 TIME 建树的完整输入读取；只读真源，不根据 infer 或 messages 选择记忆。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyKind,
    HierarchyRole,
    HierarchyStatus,
    LifecycleState,
    MemoryUnit,
    Scope,
    memory_key,
)
from jiuwen_memory.common.type_def.hierarchy import validate_ref
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.hierarchy_composer import (
    TIME_CHILD_ROLES,
    TIME_PARENT_ROLES,
    HierarchyComposeOptions,
)
from jiuwen_memory.control.evolution.validation import scope_contains
from jiuwen_memory.storage.kv import KVStore

ScopeKey = tuple[str, str, str, str, str]
NodeKey = tuple[ScopeKey, str]


@dataclass(frozen=True)
class HierarchyJobLimits:
    """收齐候选的保护上限与锁等待时间，不改变时间切分算法。"""

    max_leaves: int = 5000
    page_size: int = 200
    lock_wait_ms: int = 30_000

    def __post_init__(self) -> None:
        for label, value in (("max_leaves", self.max_leaves), ("page_size", self.page_size)):
            if type(value) is not int or value <= 0:
                raise ValidationError(f"hierarchy {label} must be a positive integer")
        if type(self.lock_wait_ms) is not int or self.lock_wait_ms < 0:
            raise ValidationError("hierarchy lock_wait_ms must be a nonnegative integer")


@dataclass
class HierarchyCandidates:
    """备齐的两/三/四层候选；同名 id 按完整 Scope 区分。"""

    leaves: dict[NodeKey, MemoryUnit] = field(default_factory=dict)
    parents: dict[NodeKey, MemoryUnit] = field(default_factory=dict)
    attached: dict[NodeKey, MemoryUnit] = field(default_factory=dict)


def scope_key(scope: Scope) -> ScopeKey:
    """不拼接分隔符，保留五维 Scope 的无歧义身份。"""
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def node_key(unit: MemoryUnit) -> NodeKey:
    """记忆真源身份。"""
    return scope_key(unit.scope), unit.id


def _utc(value: datetime | None) -> datetime:
    if not isinstance(value, datetime):
        raise ValidationError("TIME hierarchy span must contain datetime values")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _intersects(unit: MemoryUnit, options: HierarchyComposeOptions) -> bool:
    return (
        _utc(unit.hierarchy.span_start) <= _utc(options.span_end)
        and _utc(options.span_start) <= _utc(unit.hierarchy.span_end)
    )


def _active_time(unit: MemoryUnit) -> bool:
    return (
        unit.hierarchy.kind is HierarchyKind.TIME
        and unit.hierarchy.status is HierarchyStatus.ACTIVE
        and unit.lifecycle is LifecycleState.ACTIVE
    )


def _decode(raw: bytes, stored_scope: Scope, stored_key: str) -> MemoryUnit:
    decoded = loads(raw)
    if decoded is None or not decoded.id or memory_key(decoded.id) != stored_key:
        raise ValidationError("hierarchy read returned invalid MemoryUnit identity")
    if decoded.scope != stored_scope:
        raise ValidationError("hierarchy read returned a MemoryUnit outside its exact Scope")
    encoded_ref = json.loads(raw).get("hierarchy")
    if isinstance(encoded_ref, dict) and encoded_ref.get("kind") == "time":
        if decoded.hierarchy.kind is not HierarchyKind.TIME:
            raise ValidationError("TIME hierarchy was lost during tolerant decoding")
        if not isinstance(encoded_ref.get("parent_id", ""), str):
            raise ValidationError("TIME hierarchy parent_id must be a string")
    return decoded


def read_scope_units(kv: KVStore, scope: Scope, page_size: int) -> Iterator[MemoryUnit]:
    """完整分页与精确身份校验，供显式重建和增量扫描共用。"""
    offset = 0
    expected_count: int | None = None
    seen: set[str] = set()
    while True:
        page = kv.list(scope, offset=offset, limit=page_size)
        if type(page.count) is not int or page.count < 0:
            raise ValidationError("hierarchy pagination returned an invalid count")
        if expected_count is None:
            expected_count = page.count
        if page.count != expected_count:
            raise ValidationError("hierarchy pagination count changed; retry with stable input")
        if len(page.entries) > page_size or offset + len(page.entries) > expected_count:
            raise ValidationError("hierarchy pagination entries exceed the declared count or limit")
        for stored_key, raw in page.entries:
            if stored_key in seen:
                raise ValidationError("hierarchy pagination repeated a key; input is incomplete")
            seen.add(stored_key)
            yield _decode(raw, scope, stored_key)
        offset += len(page.entries)
        if offset == expected_count:
            return
        if not page.entries:
            raise ValidationError("hierarchy pagination stopped before the declared count")


def _select(
    unit: MemoryUnit, candidates: HierarchyCandidates, options: HierarchyComposeOptions,
) -> None:
    if not _active_time(unit):
        return
    validate_ref(unit.hierarchy, unit_id=unit.id)
    if not _intersects(unit, options):
        return
    if unit.hierarchy.role is HierarchyRole.SNAPSHOT:
        if unit.hierarchy.child_ids or unit.hierarchy.child_scopes:
            raise ValidationError("snapshot must not contain child references")
        if unit.hierarchy.parent_id:
            candidates.attached[node_key(unit)] = unit
        elif unit.hierarchy.parent_scope is not None:
            raise ValidationError("snapshot has parent_scope without parent_id")
        else:
            candidates.leaves[node_key(unit)] = unit
    elif unit.hierarchy.role in TIME_PARENT_ROLES and (
        unit.scope == options.tree_home_scope
    ):
        if unit.hierarchy.role is HierarchyRole.EVENT and unit.hierarchy.parent_id:
            raise ValidationError("TIME event candidates must be root nodes")
        if not unit.hierarchy.parent_id and unit.hierarchy.parent_scope is not None:
            raise ValidationError("TIME parent_scope requires parent_id")
        if not unit.hierarchy.child_ids:
            raise ValidationError("TIME parent candidate has no children")
        candidates.parents[node_key(unit)] = unit


def _check_limit(candidates: HierarchyCandidates, max_leaves: int) -> None:
    if len(candidates.leaves) > max_leaves:
        raise ValidationError(f"hierarchy candidate leaves exceed max_leaves={max_leaves}")
    if len(candidates.parents) > len(TIME_PARENT_ROLES) * max_leaves:
        raise ValidationError(f"hierarchy candidate parents exceed 3 * max_leaves={max_leaves}")


def _child_requests(
    parents: dict[NodeKey, MemoryUnit], home: Scope,
) -> dict[ScopeKey, tuple[Scope, dict[str, MemoryUnit]]]:
    """在任何点读前检查所有引用边界，再按完整 Scope 分批。"""
    requests: dict[ScopeKey, tuple[Scope, dict[str, MemoryUnit]]] = {}
    for parent in parents.values():
        for index, child_id in enumerate(parent.hierarchy.child_ids):
            child_scope = parent.hierarchy.child_scope_at(index, parent.scope)
            if not scope_contains(home, child_scope):
                raise ValidationError("hierarchy child reference is outside the authorized Scope")
            if parent.hierarchy.role is not HierarchyRole.TIME_SPAN and child_scope != home:
                raise ValidationError("parent child must reside in exact tree_home_scope")
            entry = requests.setdefault(scope_key(child_scope), (child_scope, {}))
            if child_id in entry[1]:
                raise ValidationError("hierarchy child is claimed by more than one old parent")
            entry[1][child_id] = parent
    return requests


def _complete_children(
    kv: KVStore, candidates: HierarchyCandidates, options: HierarchyComposeOptions,
    limits: HierarchyJobLimits,
) -> None:
    # 先校验已列出的全部边，再按 event→scene→time_span→snapshot 补齐；不越权点读。
    _child_requests(candidates.parents, options.tree_home_scope)
    for role in reversed(TIME_PARENT_ROLES):
        level = {key: parent for key, parent in candidates.parents.items()
                 if parent.hierarchy.role is role}
        requests = _child_requests(level, options.tree_home_scope)
        _read_children(kv, candidates, requests, limits)
    _validate_complete(candidates)


def _read_children(
    kv: KVStore, candidates: HierarchyCandidates,
    requests: dict[ScopeKey, tuple[Scope, dict[str, MemoryUnit]]], limits: HierarchyJobLimits,
) -> None:
    required_count = sum(len(requested_children) for _, requested_children in requests.values())
    if required_count + len(candidates.leaves) > limits.max_leaves:
        raise ValidationError(f"hierarchy candidate leaves exceed max_leaves={limits.max_leaves}")
    for child_scope, references in requests.values():
        child_ids = list(references)
        for offset in range(0, len(child_ids), limits.page_size):
            selected_ids = child_ids[offset:offset + limits.page_size]
            keys = [memory_key(child_id) for child_id in selected_ids]
            raw_values = kv.mget(child_scope, keys)
            if len(raw_values) != len(keys):
                raise ValidationError("hierarchy child mget did not return every requested child")
            for requested_id, raw_value in zip(selected_ids, raw_values):
                child = _decode(raw_value, child_scope, memory_key(requested_id))
                _validate_child(child, references[requested_id])
                target = candidates.leaves if child.hierarchy.role is HierarchyRole.SNAPSHOT else (
                    candidates.parents
                )
                previous = target.get(node_key(child))
                if previous is not None and previous != child:
                    raise ValidationError("hierarchy child changed between listing and point read")
                target[node_key(child)] = child
    _check_limit(candidates, limits.max_leaves)


def _validate_complete(candidates: HierarchyCandidates) -> None:
    for attached_key, attached_leaf in candidates.attached.items():
        expected_parent = (
            scope_key(attached_leaf.hierarchy.resolved_parent_scope(attached_leaf.scope)),
            attached_leaf.hierarchy.parent_id,
        )
        if expected_parent not in candidates.parents or attached_key not in candidates.leaves:
            raise ValidationError("snapshot references an unknown or incomplete old parent")
        if attached_leaf != candidates.leaves[attached_key]:
            raise ValidationError("snapshot changed between listing and point read")
    for parent in candidates.parents.values():
        if not parent.hierarchy.parent_id:
            continue
        parent_scope = parent.hierarchy.resolved_parent_scope(parent.scope)
        root = candidates.parents.get((scope_key(parent_scope), parent.hierarchy.parent_id))
        if root is None or TIME_CHILD_ROLES.get(root.hierarchy.role) is not parent.hierarchy.role:
            raise ValidationError("TIME node references an unknown or unsupported parent")
        declared = {(scope_key(root.hierarchy.child_scope_at(position, root.scope)), uid)
                    for position, uid in enumerate(root.hierarchy.child_ids)}
        if node_key(parent) not in declared:
            raise ValidationError("TIME parent does not declare the referencing child")


def _validate_child(child: MemoryUnit, parent: MemoryUnit) -> None:
    expected_role = TIME_CHILD_ROLES[parent.hierarchy.role]
    if not _active_time(child) or child.hierarchy.role is not expected_role:
        raise ValidationError("hierarchy old parent child has an inactive or invalid TIME role")
    validate_ref(child.hierarchy, unit_id=child.id)
    if expected_role is HierarchyRole.SNAPSHOT:
        if child.hierarchy.child_ids or child.hierarchy.child_scopes:
            raise ValidationError("snapshot child must not contain child references")
    elif not child.hierarchy.child_ids:
        raise ValidationError("TIME parent child must contain its direct children")
    if (
        child.hierarchy.parent_id != parent.id
        or child.hierarchy.resolved_parent_scope(child.scope) != parent.scope
    ):
        raise ValidationError("hierarchy child reverse parent reference is inconsistent")
    if (
        _utc(child.hierarchy.span_start) < _utc(parent.hierarchy.span_start)
        or _utc(child.hierarchy.span_end) > _utc(parent.hierarchy.span_end)
    ):
        raise ValidationError("hierarchy old parent does not cover its child span")


def collect_candidates(
    kv: KVStore, scope: Scope, options: HierarchyComposeOptions, limits: HierarchyJobLimits,
) -> HierarchyCandidates:
    """完整分页收集新叶与相交旧根，补齐 event→scene→time_span→snapshot 整棵旧子树。"""
    scoped: dict[ScopeKey, Scope] = {scope_key(scope): scope}
    for stored_scope in kv.scopes():
        if scope_contains(scope, stored_scope):
            scoped[scope_key(stored_scope)] = stored_scope
    collected = HierarchyCandidates()
    for selected_key in sorted(scoped):
        for candidate in read_scope_units(kv, scoped[selected_key], limits.page_size):
            _select(candidate, collected, options)
            _check_limit(collected, limits.max_leaves)
    _complete_children(kv, collected, options, limits)
    if collected.parents:
        _validate_reverse_claims(kv, scoped, collected, limits.page_size)
    return collected


def _validate_reverse_claims(
    kv: KVStore, scopes: dict[ScopeKey, Scope], candidates: HierarchyCandidates, page_size: int,
) -> None:
    """再次流式核对入边，防止区间外漏列节点仍指向即将退役的旧父。"""
    nodes = {**candidates.leaves, **candidates.parents}
    verified: set[NodeKey] = set()
    for selected_scope in scopes.values():
        for observed in read_scope_units(kv, selected_scope, page_size):
            key = node_key(observed)
            previous = nodes.get(key)
            if previous is not None:
                if previous != observed:
                    raise ValidationError("hierarchy candidate changed during completeness check")
                verified.add(key)
                continue
            if not _active_time(observed):
                continue
            ref = observed.hierarchy
            if ref.parent_id:
                parent_key = (scope_key(ref.resolved_parent_scope(observed.scope)), ref.parent_id)
                if parent_key in candidates.parents:
                    raise ValidationError("old subtree omitted an outside-window reverse child")
            for position, child_id in enumerate(ref.child_ids):
                child_key = (scope_key(ref.child_scope_at(position, observed.scope)), child_id)
                if child_key in nodes:
                    raise ValidationError("outside old parent claims a selected subtree node")
    if len(verified) != len(nodes):
        raise ValidationError("hierarchy candidate disappeared during completeness check")
