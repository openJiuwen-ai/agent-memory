# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""单/跨空间共用的展开收尾：先确定根，再共用一个披露预算。

PreparedRetrievalResult 仅在内部 defer_expansion 协议中流转；公开返回值仍是
普通 RetrievalResult。准备阶段不读取后代，跨空间仅把最终选中的根交给本模块。
正文仍由注入的 Discloser 渲染；预算按实际渲染字段估算，不把原始全文冒充 L0 成本。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter

from jiuwen_memory.common.errors import BackendError, UnsupportedCapabilityError
from jiuwen_memory.common.type_def import MemoryUnit, ParsedQuery, Scope, ScoredMemoryUnit

from .discloser import Discloser
from .expander import MAX_EXPANSION_NODES, Expander, ExpandRequest, ExpandResult, NodeKey, node_key
from .types import (
    ChannelError,
    DisclosureLevel,
    RecallChannel,
    RetrievalQuery,
    RetrievalResult,
    RetrievedItem,
    TrajectoryStep,
)


@dataclass(frozen=True)
class ExpansionSource:
    """一条已授权查询的展开依赖；不携带 actor，不重新鉴权或召回。"""

    scope: Scope
    query: ParsedQuery
    expander: Expander
    discloser: Discloser


@dataclass(frozen=True)
class ExpansionRoot:
    """保留物化根与来源，合并阶段不依赖裸 id 推测所属空间。"""

    item: RetrievedItem
    candidate: ScoredMemoryUnit
    source: ExpansionSource


@dataclass
class PreparedRetrievalResult(RetrievalResult):
    """内部中间结果，不得直接交付给 API 调用方或序列化。"""

    expansion_roots: list[ExpansionRoot] = field(default_factory=list)


@dataclass
class DisclosureBudget:
    """主披露字段的逻辑 token 池；不是包含所有三层字段的 JSON 字节上限。"""

    remaining: int | None
    spent: int = 0

    def take(self, item: RetrievedItem, level: DisclosureLevel) -> RetrievedItem | None:
        """固定层级不降级；ADAPTIVE 按 L2→L1→L0 选择当前能容纳的主层级。"""
        if level is DisclosureLevel.ADAPTIVE:
            levels = (
                (DisclosureLevel.L1, DisclosureLevel.L0) if self.remaining is None
                else (DisclosureLevel.L2, DisclosureLevel.L1, DisclosureLevel.L0)
            )
        else:
            levels = (level,)
        for candidate_level in levels:
            cost = disclosed_tokens(item, candidate_level)
            if self.remaining is not None and cost > self.remaining:
                continue
            if self.remaining is not None:
                self.remaining -= cost
            self.spent += cost
            return replace(item, level=candidate_level)
        return None


def disclosed_tokens(item: RetrievedItem, level: DisclosureLevel | None = None) -> int:
    """每四个字符估算一个 token，空主字段也计一个准入单位。"""
    selected_level = item.level if level is None else level
    text = {
        DisclosureLevel.L0: item.abstract,
        DisclosureLevel.L1: item.overview,
        DisclosureLevel.L2: item.content,
    }[selected_level]
    return max(1, (len(text) + 3) // 4)


def prepare_expansion(
    result: RetrievalResult, source: ExpansionSource, candidates: list[ScoredMemoryUnit],
) -> PreparedRetrievalResult:
    """捕获根及其来源；只有完成全局选根后才调用展开算子。"""
    by_id = {candidate.unit_id: candidate for candidate in candidates}
    prepared = PreparedRetrievalResult(
        items=result.items, trajectory=result.trajectory, errors=result.errors,
    )
    for item in result.items:
        candidate = by_id.get(item.unit_id)
        if candidate is None:
            raise BackendError("Discloser 返回了候选集之外的展开根")
        prepared.expansion_roots.append(ExpansionRoot(item, candidate, source))
    return prepared


@dataclass
class _Completion:
    query: RetrievalQuery
    result: RetrievalResult
    budget: DisclosureBudget
    seen: set[NodeKey] = field(default_factory=set)
    remaining_nodes: int = MAX_EXPANSION_NODES

    def select(self, root: ExpansionRoot, unit: MemoryUnit, depth: int) -> bool:
        """逐节点塑形避免跨 Scope 同 id 覆盖；展开分数仅继承根，不伪造通道证据。"""
        candidate = ScoredMemoryUnit(unit, root.item.score, root.candidate.channel)
        rendered = root.source.discloser.disclose(
            root.source.query, [candidate], {unit.id: unit}, DisclosureLevel.L0,
        )
        if len(rendered) != 1 or rendered[0].unit_id != unit.id:
            raise BackendError("Discloser 未返回对应的展开节点")
        item = self.budget.take(rendered[0], self.query.disclosure)
        if item is None:
            return False
        self.result.items.append(replace(item, score=root.item.score))
        return True

    def expand(self, root: ExpansionRoot) -> None:
        """根按最终命中顺序执行 BFS；共享节点上限和预算不会在空间边界重置。"""
        started = perf_counter()
        unit = root.candidate.unit
        if self.budget.remaining == 0 and unit.hierarchy.child_ids:
            outcome = ExpandResult()
            outcome.exclude("budget_exhausted", truncated=True)
        else:
            outcome = root.source.expander.expand(
                root.source.scope,
                ExpandRequest(
                    root=unit,
                    query=root.source.query,
                    depth=self.query.expand_depth,
                    select=lambda child, depth: self.select(root, child, depth),
                    seen=self.seen,
                    node_limit=self.remaining_nodes,
                ),
            )
        self.remaining_nodes -= outcome.visited_count
        self.record(root, outcome, (perf_counter() - started) * 1000)

    def record(self, root: ExpansionRoot, outcome: ExpandResult, cost_ms: float) -> None:
        """缺子、拒绝与截断在 errors 中可见，轨迹开关不会隐藏不完整状态。"""
        for issue in outcome.issues:
            self.result.errors.append(ChannelError(
                channel=RecallChannel.HIERARCHY,
                source="expand",
                error_type=issue.code,
                message=f"root={root.item.unit_id}: {issue.code}",
            ))
        if self.query.with_trajectory:
            self.result.trajectory.append(TrajectoryStep(
                stage="expand", cost_ms=cost_ms, candidate_count=outcome.selected_count,
                detail={
                    "root_id": root.item.unit_id,
                    "root_scope": json.dumps(asdict(root.candidate.unit.scope), sort_keys=True),
                    "kind": root.source.query.hierarchy_kind.value,
                    "requested_depth": str(self.query.expand_depth),
                    "actual_depth": str(outcome.actual_depth),
                    "item_count": str(outcome.selected_count),
                    "visited_count": str(outcome.visited_count),
                    "truncated": str(outcome.truncated).lower(),
                    "complete": str(outcome.complete).lower(),
                    "issues": ",".join(issue.code for issue in outcome.issues),
                    "estimated_tokens": str(self.budget.spent),
                },
            ))


def complete_expansion(
    selected: RetrievalResult, prepared: list[PreparedRetrievalResult], query: RetrievalQuery,
) -> RetrievalResult:
    """选中根先消耗预算，随后按根顺序展开；仅返回普通扁平 RetrievalResult。"""
    roots_by_item: dict[int, ExpansionRoot] = {}
    for result in prepared:
        for root in result.expansion_roots:
            roots_by_item[id(root.item)] = root
    output = RetrievalResult(trajectory=list(selected.trajectory), errors=list(selected.errors))
    state = _Completion(query, output, DisclosureBudget(query.max_tokens))
    roots: list[ExpansionRoot] = []
    for item in selected.items:
        root = roots_by_item.get(id(item))
        if root is None:
            raise UnsupportedCapabilityError(
                "defer_expansion", "true", "Retriever",
                "检索实现未提供延迟展开的物化根",
            )
        key = node_key(root.candidate.unit)
        if key in state.seen:
            continue
        admitted = state.budget.take(item, query.disclosure)
        if admitted is None:
            outcome = ExpandResult()
            outcome.exclude("budget_exhausted", truncated=True)
            state.record(root, outcome, 0.0)
            break
        output.items.append(admitted)
        roots.append(root)
        state.seen.add(key)
    for root in roots:
        state.expand(root)
    return output
