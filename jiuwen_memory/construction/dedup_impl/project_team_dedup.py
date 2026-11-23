# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project facts and team conclusions occupy separate comparison ranges."""
from __future__ import annotations

from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    LifecycleState,
    MemoryUnit,
)
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.common.type_def.memory_filter import matches_memory_unit
from jiuwen_memory.construction.dedup import DedupProducer
from jiuwen_memory.construction.dedup_impl.shadow_dedup import (
    ShadowDedup,
    build_shadow_dedup,
    candidate_project,
)
from jiuwen_memory.storage.markdown_impl._md_blocks import content_hash, document_line
from jiuwen_memory.storage.types import FilterExpr


class ProjectTeamDedup(ShadowDedup):
    """Reuse similarity recall while separating conclusions from ordinary facts."""

    @classmethod
    def contains_exact(cls, shadow, candidate: MemoryUnit) -> bool:
        content = document_line(candidate.content)
        ids = shadow.find_unit_ids_by_content_hash(candidate.scope, content_hash(content))
        filters = cls.recall_filter(candidate)
        return any(
            old.content == content and matches_memory_unit(old, filters)
            for old in shadow.get_units(candidate.scope, ids)
        )

    @staticmethod
    def recall_filter(candidate: MemoryUnit) -> FilterExpr | None:
        project = candidate_project(candidate)
        clauses = [FilterClause("lifecycle", FilterOp.IN, [LifecycleState.ACTIVE.value])]
        metadata = candidate.system_metadata or {}
        destination = metadata.get("collective_destination")
        memory_class = metadata.get("memory_class", "")
        destination_field = "system_metadata.collective_destination"
        class_field = "system_metadata.memory_class"
        if destination:
            clauses.extend(
                FilterClause(f"system_metadata.collective.{dimension}", FilterOp.EQ, metadata.get(dimension, ""))
                for dimension in ("project", "team", "group")
            )
            clauses.append(FilterClause(class_field, FilterOp.EQ, memory_class))
            if memory_class == "daily_memory":
                clauses.append(FilterClause(destination_field, FilterOp.EQ, destination))
            clauses.append(FilterClause(
                "system_metadata.collective.shareable", FilterOp.EQ, metadata.get("shareable", False),
            ))
        elif metadata.get("memory_class") == "team_memory":
            clauses.extend([
                FilterClause("system_metadata.project", FilterOp.EQ, project),
                FilterClause("system_metadata.team", FilterOp.EQ, metadata.get("team", "")),
                FilterClause("system_metadata.memory_class", FilterOp.EQ, "team_memory"),
            ])
        else:
            clauses.extend([
                FilterClause(class_field, FilterOp.NE, "team_memory"),
                FilterGroup(FilterLogic.OR, [
                    FilterClause(class_field, FilterOp.NE, "daily_memory"),
                    FilterClause(destination_field, FilterOp.EQ, ""),
                ]),
            ])
            if project:
                clauses.append(FilterClause("system_metadata.project", FilterOp.IN, ["", project]))
        return and_merge(None, clauses)


@DedupProducer.register("project_team")
def _build(config):
    return build_shadow_dedup(config, ProjectTeamDedup)
