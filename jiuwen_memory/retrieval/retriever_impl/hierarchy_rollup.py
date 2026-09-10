# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""有界祖先点读与 MaxP；仅消费已经融合/精排的同尺度候选，不扫描或写树。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    Scope,
    ScoredMemoryUnit,
    is_retrieval_candidate,
)
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery
from jiuwen_memory.retrieval.expander import NodeKey, node_key, within_scope
from jiuwen_memory.storage.domain_store import DomainStore

MAX_ROLLUP_HOPS = 32
MAX_ROLLUP_NODES = 1000


@dataclass(frozen=True)
class RollupRequest:
    """query 不含 typed 输出角色，仍保留所有业务/权限/时间可见性条件。"""

    scope: Scope
    query: ParsedQuery
    target_role: HierarchyRole | None
    candidates: list[ScoredMemoryUnit]
    max_hops: int = MAX_ROLLUP_HOPS
    node_limit: int = MAX_ROLLUP_NODES


@dataclass
class RollupResult:
    """稳定诊断不暴露被排除节点的身份、正文或后端异常信息。"""

    candidates: list[ScoredMemoryUnit] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    visited_count: int = 0
    admitted_count: int = 0
    boosted_count: int = 0

    def exclude(self, code: str) -> None:
        """单请求按首次出现顺序去重诊断码。"""
        if code not in self.issues:
            self.issues.append(code)


@dataclass
class _Rollup:
    domain: DomainStore
    request: RollupRequest
    result: RollupResult
    cache: dict[NodeKey, MemoryUnit | None] = field(default_factory=dict)
    selected: dict[NodeKey, ScoredMemoryUnit] = field(default_factory=dict)

    def visible(self, unit: MemoryUnit) -> bool:
        if not within_scope(unit.scope, self.request.scope):
            self.result.exclude("scope_excluded")
            return False
        try:
            allowed = is_retrieval_candidate(
                unit, self.request.query, filters=self.request.query.recheck_filters,
            )
        except (ValidationError, TypeError, ValueError, AttributeError):
            self.result.exclude("invalid_structure")
            return False
        if not allowed:
            self.result.exclude("visibility_excluded")
        return allowed

    def load(self, scope: Scope, unit_id: str) -> MemoryUnit | None:
        key = (scope.org, scope.space, scope.user, scope.agent, scope.session, unit_id)
        if key not in self.cache:
            self.cache[key] = None
            try:
                units = self.domain.get(scope, [unit_id])
            except NotFoundError:
                self.result.exclude("missing_parent")
                return None
            except Exception:
                self.result.exclude("read_error")
                return None
            for unit in units:
                if unit.scope == scope and unit.id == unit_id:
                    self.cache[key] = unit
                    break
            if self.cache[key] is None:
                self.result.exclude("missing_parent")
        return self.cache[key]

    def ancestor(self, source: MemoryUnit) -> MemoryUnit | None:
        current = source
        seen = {node_key(source)}
        for _ in range(self.request.max_hops):
            reference = current.hierarchy
            if not reference.parent_id:
                return None
            if self.result.visited_count >= self.request.node_limit:
                self.result.exclude("node_limit")
                return None
            self.result.visited_count += 1
            parent_scope = reference.resolved_parent_scope(current.scope)
            # 先判授权范围，禁止先读取越界节点再丢弃。
            if not within_scope(parent_scope, self.request.scope):
                self.result.exclude("scope_excluded")
                return None
            parent = self.load(parent_scope, reference.parent_id)
            if parent is None:
                return None
            key = node_key(parent)
            if key in seen:
                self.result.exclude("cycle")
                return None
            if not self.visible(parent):
                return None
            if not _contains_child(parent, current):
                self.result.exclude("child_mismatch")
                return None
            if not _covers_span(parent, current):
                self.result.exclude("span_not_covered")
                return None
            target_role = self.request.target_role
            if target_role is None or parent.hierarchy.role is target_role:
                return parent
            seen.add(key)
            current = parent
        if current.hierarchy.parent_id:
            self.result.exclude("hop_limit")
        return None

    def promote(self, source: ScoredMemoryUnit) -> None:
        role = self.request.target_role
        if role is not None and source.unit.hierarchy.role is role:
            return
        parent = self.ancestor(source.unit)
        if parent is None:
            return
        key = node_key(parent)
        previous = self.selected.get(key)
        if previous is None:
            # 不把子命中的通道 evidence 冒充父自身的索引证据。
            self.selected[key] = ScoredMemoryUnit(parent, source.score, source.channel)
        elif source.score > previous.score:
            self.selected[key] = replace(previous, score=source.score)


def rollup_candidates(domain: DomainStore, request: RollupRequest) -> RollupResult:
    """先保留直接命中，再按完整身份准入祖先，最终取最高分稳定排序。"""
    if not HierarchyQuery.from_query(request.query).enabled:
        raise ValidationError("上卷要求显式 hierarchy_kind")
    if request.query.hierarchy_role is not None:
        raise ValidationError("上卷候选查询不得限定输出 hierarchy_role")
    if type(request.max_hops) is not int or request.max_hops < 1:
        raise ValidationError("上卷 max_hops 必须是正整数")
    if type(request.node_limit) is not int or request.node_limit < 0:
        raise ValidationError("上卷 node_limit 必须是非负整数")
    result = RollupResult()
    state = _Rollup(domain, request, result)
    sources: list[ScoredMemoryUnit] = []
    for candidate in request.candidates:
        if not isfinite(candidate.score):
            result.exclude("invalid_score")
            continue
        if not state.visible(candidate.unit):
            continue
        key = node_key(candidate.unit)
        state.cache[key] = candidate.unit
        sources.append(candidate)
        if request.target_role is None or candidate.unit.hierarchy.role is request.target_role:
            previous = state.selected.get(key)
            if previous is None or candidate.score > previous.score:
                state.selected[key] = candidate
    direct_scores = {
        direct_key: direct_candidate.score
        for direct_key, direct_candidate in state.selected.items()
    }
    for candidate in sources:
        state.promote(candidate)
    result.candidates = sorted(state.selected.values(), key=lambda item: item.score, reverse=True)
    result.admitted_count = len(state.selected.keys() - direct_scores.keys())
    result.boosted_count = sum(
        state.selected[score_key].score > score for score_key, score in direct_scores.items()
    )
    return result


def _contains_child(parent: MemoryUnit, child: MemoryUnit) -> bool:
    reference = parent.hierarchy
    for index, unit_id in enumerate(reference.child_ids):
        if unit_id == child.id and reference.child_scope_at(index, parent.scope) == child.scope:
            return True
    return False


def _covers_span(parent: MemoryUnit, child: MemoryUnit) -> bool:
    parent_span = HierarchyQuery(
        parent.hierarchy.kind, span_start=parent.hierarchy.span_start,
        span_end=parent.hierarchy.span_end,
    )
    child_span = HierarchyQuery(
        child.hierarchy.kind, span_start=child.hierarchy.span_start,
        span_end=child.hierarchy.span_end,
    )
    if parent_span.span_start is None or child_span.span_start is None:
        return True
    return (
        parent_span.span_start <= child_span.span_start
        and parent_span.span_end >= child_span.span_end
    )
