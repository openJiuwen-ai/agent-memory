# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""有界 BFS：先检查 Scope 再点读，真源校验通过后才披露子节点。

每批至多读取 64 个引用，单次请求的 node_limit 由外层共享节点上限收窄。
visited_count 计引用尝试数，坏引用与重复引用也占限额，避免坏树绕过保护。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.type_def import (
    HierarchyRef,
    HierarchyStatus,
    MemoryUnit,
    Scope,
    is_retrieval_candidate,
)
from jiuwen_memory.common.type_def.hierarchy import validate_ref
from jiuwen_memory.common.type_def.hierarchy_query import HierarchyQuery, matches_hierarchy
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.expander import (
    Expander,
    ExpanderProducer,
    ExpandRequest,
    ExpandResult,
    NodeKey,
    node_key,
    within_scope,
)
from jiuwen_memory.storage.domain_store import DomainStore

_READ_BATCH_SIZE = 64


@dataclass
class _Walk:
    scope: Scope
    request: ExpandRequest
    result: ExpandResult


@dataclass
class _Parent:
    unit: MemoryUnit
    ancestors: frozenset[NodeKey]


class DefaultExpander(Expander):
    """只使用 Retriever 注入的同一数据面，不扫描 Scope 或其他节点。"""

    def __init__(self, domain_store: DomainStore) -> None:
        self._domain = domain_store

    @staticmethod
    def operator_type() -> RetrievalOperatorType:
        return RetrievalOperatorType.EXPANDER

    @staticmethod
    def health() -> None:
        return None

    def expand(self, scope: Scope, request: ExpandRequest) -> ExpandResult:
        """根已物化；在请求范围内按层处理有序子引用，坏分支互不影响。"""
        if type(request.depth) is not int or request.depth < 1:
            raise ValidationError("展开 depth 必须是正整数")
        if type(request.node_limit) is not int or request.node_limit < 0:
            raise ValidationError("展开 node_limit 必须是非负整数")
        if not within_scope(request.root.scope, scope):
            raise NotFoundError("展开根不在当前范围内")
        hierarchy = HierarchyQuery.from_query(request.query)
        if not hierarchy.enabled or not matches_hierarchy(request.root, hierarchy):
            raise ValidationError("展开根的结构与查询不匹配")
        result = ExpandResult()
        state = _Walk(scope, request, result)
        root_key = node_key(request.root)
        request.seen.add(root_key)
        frontier = [_Parent(request.root, frozenset({root_key}))]
        for depth in range(1, request.depth + 1):
            frontier = self._level(frontier, depth, state)
            if result.truncated or not frontier:
                break
        return result

    def _level(self, parents: list[_Parent], depth: int, state: _Walk) -> list[_Parent]:
        admitted: list[_Parent] = []
        for parent in parents:
            for child in self._children(parent.unit, state):
                if not self._admits(child, parent, state):
                    continue
                if not state.request.select(child, depth):
                    state.result.exclude("budget_exhausted", truncated=True)
                    return admitted
                key = node_key(child)
                state.request.seen.add(key)
                admitted.append(_Parent(child, parent.ancestors | {key}))
                state.result.actual_depth = depth
                state.result.selected_count += 1
            if state.result.truncated:
                break
        return admitted

    def _children(self, parent: MemoryUnit, state: _Walk) -> Iterator[MemoryUnit]:
        reference = parent.hierarchy
        for offset in range(0, len(reference.child_ids), _READ_BATCH_SIZE):
            remaining = state.request.node_limit - state.result.visited_count
            if remaining <= 0:
                state.result.exclude("node_limit", truncated=True)
                return
            stop = min(offset + _READ_BATCH_SIZE, len(reference.child_ids), offset + remaining)
            wanted: list[tuple[Scope, str]] = []
            for index in range(offset, stop):
                state.result.visited_count += 1
                child_scope = reference.child_scope_at(index, parent.scope)
                if not within_scope(child_scope, state.scope):
                    state.result.exclude("scope_excluded")
                    continue
                wanted.append((child_scope, reference.child_ids[index]))
            yield from self._read(wanted, state.result)
            if stop < min(offset + _READ_BATCH_SIZE, len(reference.child_ids)):
                state.result.exclude("node_limit", truncated=True)
                return

    def _read(
        self, wanted: list[tuple[Scope, str]], result: ExpandResult,
    ) -> Iterator[MemoryUnit]:
        """按 Scope 分批点读，再恢复父声明的顺序；错误不回显外部数据。"""
        grouped: dict[tuple[str, ...], tuple[Scope, list[str]]] = {}
        for scope, unit_id in wanted:
            key = (scope.org, scope.space, scope.user, scope.agent, scope.session)
            grouped.setdefault(key, (scope, []))[1].append(unit_id)
        fetched: dict[NodeKey, MemoryUnit] = {}
        for scope, unit_ids in grouped.values():
            units = self._load(scope, list(dict.fromkeys(unit_ids)), result)
            for unit in units:
                if unit.scope == scope and unit.id in unit_ids:
                    fetched[node_key(unit)] = unit
        for scope, unit_id in wanted:
            key = (scope.org, scope.space, scope.user, scope.agent, scope.session, unit_id)
            child = fetched.get(key)
            if child is None:
                result.exclude("missing_child")
            else:
                yield child

    def _load(self, scope: Scope, unit_ids: list[str], result: ExpandResult) -> list[MemoryUnit]:
        try:
            return self._domain.get(scope, unit_ids)
        except NotFoundError:
            # 不同数据面的缺失语义不同：批次缺一报错时退回逐项，保留健康兄弟。
            found: list[MemoryUnit] = []
            for unit_id in unit_ids:
                try:
                    found.extend(self._domain.get(scope, [unit_id]))
                except NotFoundError:
                    result.exclude("missing_child")
                except Exception:
                    result.exclude("read_error")
            return found
        except Exception:
            result.exclude("read_error")
            return []

    @staticmethod
    def _admits(child: MemoryUnit, parent: _Parent, state: _Walk) -> bool:
        key = node_key(child)
        if key in parent.ancestors:
            state.result.exclude("cycle")
            return False
        reference = child.hierarchy
        if not isinstance(reference, HierarchyRef):
            state.result.exclude("invalid_structure")
            return False
        if reference.kind is not state.request.query.hierarchy_kind:
            state.result.exclude("kind_mismatch")
            return False
        if reference.status is not HierarchyStatus.ACTIVE:
            state.result.exclude("status_excluded")
            return False
        if (
            reference.parent_id != parent.unit.id
            or reference.resolved_parent_scope(child.scope) != parent.unit.scope
        ):
            state.result.exclude("parent_mismatch")
            return False
        try:
            validate_ref(reference, unit_id=child.id)
            if not _covered_by(child, parent.unit):
                state.result.exclude("span_not_covered")
                return False
            query = state.request.query
            if not is_retrieval_candidate(child, query, filters=query.recheck_filters):
                state.result.exclude("visibility_excluded")
                return False
        except (ValidationError, TypeError, ValueError, AttributeError):
            state.result.exclude("invalid_structure")
            return False
        # 根也可能是另一个根的后代；已选节点保留首次位置，不是错误或环。
        return key not in state.request.seen


def _covered_by(child: MemoryUnit, parent: MemoryUnit) -> bool:
    child_span = HierarchyQuery(
        child.hierarchy.kind, span_start=child.hierarchy.span_start,
        span_end=child.hierarchy.span_end,
    )
    parent_span = HierarchyQuery(
        parent.hierarchy.kind, span_start=parent.hierarchy.span_start,
        span_end=parent.hierarchy.span_end,
    )
    if child_span.span_start is None or parent_span.span_start is None:
        return True
    return (
        parent_span.span_start <= child_span.span_start
        and parent_span.span_end >= child_span.span_end
    )


@ExpanderProducer.register("default")
def _build(config):
    domain = config.get("domain_store")
    if not isinstance(domain, DomainStore):
        raise ValidationError("Expander 必须由 Retriever 注入同一 DomainStore 实例")
    return DefaultExpander(domain)
