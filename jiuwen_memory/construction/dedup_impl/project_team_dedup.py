# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project facts and team conclusions occupy separate comparison ranges."""
from __future__ import annotations

from jiuwen_memory.common.type_def import FilterClause, FilterOp, LifecycleState, MemoryUnit
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.dedup_impl.shadow_dedup import (
    ShadowDedup,
    build_shadow_dedup,
    candidate_project,
)
from jiuwen_memory.storage.types import FilterExpr


class ProjectTeamDedup(ShadowDedup):
    """Reuse similarity recall while separating conclusions from ordinary facts."""

    @staticmethod
    def recall_filter(candidate: MemoryUnit) -> FilterExpr | None:
        project = candidate_project(candidate)
        clauses = [FilterClause("lifecycle", FilterOp.IN, [LifecycleState.ACTIVE.value])]
        metadata = candidate.system_metadata or {}
        if metadata.get("memory_class") == "team_memory":
            clauses.extend([
                FilterClause("system_metadata.project", FilterOp.EQ, project),
                FilterClause("system_metadata.team", FilterOp.EQ, metadata.get("team", "")),
                FilterClause("system_metadata.memory_class", FilterOp.EQ, "team_memory"),
            ])
        else:
            clauses.append(FilterClause("system_metadata.memory_class", FilterOp.NE, "team_memory"))
            if project:
                clauses.append(FilterClause("system_metadata.project", FilterOp.IN, ["", project]))
        return and_merge(None, clauses)


@DedupProducer.register("project_team")
def _build(config):
    return build_shadow_dedup(config, ProjectTeamDedup)
