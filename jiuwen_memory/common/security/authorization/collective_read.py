# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build a bounded recall plan from exact memberships and trusted current dimensions."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.common.type_def.filter import (
    FilterClause,
    FilterExpr,
    FilterGroup,
    FilterLogic,
    FilterOp,
)

if TYPE_CHECKING:
    from jiuwen_memory.storage.collective_members import Dimension

COLLECTIVE_MEMBER_LIMIT = 64
ORDINARY_VISIBILITY = FilterClause("system_metadata.collective.shareable", FilterOp.EQ, True)


@dataclass(frozen=True)
class MemberTarget:
    scope: Scope
    filters: FilterExpr


@dataclass(frozen=True)
class CollectiveReadPlan:
    targets: tuple[MemberTarget, ...]
    omitted: int


def _any(children: list[FilterExpr]) -> FilterExpr:
    return children[0] if len(children) == 1 else FilterGroup(FilterLogic.OR, children)


def _dimension_filter(dimension: Dimension) -> FilterExpr:
    clause = FilterClause(f"system_metadata.collective.{dimension.kind}", FilterOp.EQ, dimension.id)
    if dimension.kind != "project":
        return FilterGroup(FilterLogic.AND, [
            clause,
            FilterClause("system_metadata.collective_destination", FilterOp.EQ, dimension.kind),
        ])
    return FilterGroup(FilterLogic.AND, [
        clause,
        FilterClause("system_metadata.collective.team", FilterOp.EQ, ""),
        FilterClause("system_metadata.collective.group", FilterOp.EQ, ""),
    ])


def own_visibility(current: Collection[Dimension]) -> FilterExpr:
    return _any([ORDINARY_VISIBILITY, *[_dimension_filter(d) for d in sorted(current)]])


def read_targets(
    memberships: tuple[tuple[Dimension, Scope], ...],
    caller: Scope,
    current: Collection[Dimension],
    *,
    limit: int,
) -> CollectiveReadPlan:
    """An active caller can read current dimensions and other shareable contributions."""
    caller_key = (caller.user, caller.agent, caller.session)
    home = (caller.org, caller.space)
    dimensions_by_scope: dict[tuple[str, str, str], set[Dimension]] = {caller_key: set()}
    for dimension, contributor in memberships:
        if (
            (dimension.org, dimension.space) != home
            or (contributor.org, contributor.space) != home
            or contributor.user != caller.user
        ):
            continue
        key = (contributor.user, contributor.agent, contributor.session)
        dimensions_by_scope.setdefault(key, set()).add(dimension)
    active_current = dimensions_by_scope[caller_key].intersection(current)
    projects = ["", *(d.id for d in current if d.kind == "project")]
    project_match = FilterClause("system_metadata.collective.project", FilterOp.IN, projects)
    peers = sorted(
        ((key, dims) for key, dims in dimensions_by_scope.items() if key != caller_key),
        key=lambda pair: (not bool(pair[1] & active_current), pair[0]),
    )
    result = [MemberTarget(
        caller, FilterGroup(FilterLogic.AND, [project_match, own_visibility(active_current)]),
    )]
    for (user, agent, session), dims in peers[: max(0, limit - 1)]:
        here = dims & active_current
        elsewhere = dims - here
        clauses = [_dimension_filter(d) for d in sorted(here)]
        if elsewhere:
            clauses.append(
                FilterGroup(
                    FilterLogic.AND,
                    [ORDINARY_VISIBILITY, _any([_dimension_filter(d) for d in sorted(elsewhere)])],
                )
            )
        scope = Scope(org=caller.org, space=caller.space, user=user, agent=agent, session=session)
        result.append(MemberTarget(
            scope, FilterGroup(FilterLogic.AND, [project_match, _any(clauses)]),
        ))
    return CollectiveReadPlan(tuple(result), len(peers) - (len(result) - 1))
