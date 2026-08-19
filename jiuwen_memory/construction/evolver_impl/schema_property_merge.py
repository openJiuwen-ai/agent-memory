"""Schema property merge planning and execution for Evolver.

This module ports MindMemOS' optional property-merge stage without changing the
``Extractor.extract() -> list[MemoryUnit]`` contract.  Planning is read-only:
it recalls active property memories for the same schema entity and asks the LLM
for a conservative batch decision.  Execution is kept separate and is invoked
by the evolver, which remains the construction-layer persistence boundary.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    ChatMessage,
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    Segment,
)
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.construction.schema_prompts import (
    AGENT_MEMORY_PROPERTY_MERGE_APPENDIX,
    PROPERTY_DELETE_DECISION_PROMPT,
    PROPERTY_MERGE_DECISION_PROMPT,
)
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import ScoredID, VectorQuery

logger = get_logger(__name__)

_PROPERTY_MERGE_SYSTEM_PROMPT = (
    PROPERTY_MERGE_DECISION_PROMPT + AGENT_MEMORY_PROPERTY_MERGE_APPENDIX
)

_SCHEMA_MODE = "schema"
_PROPERTY_TEXT_SEPARATOR = ": "


@dataclass(slots=True)
class SchemaPropertyMergeUpdate:
    """One same-event correction planned as a non-destructive replacement."""

    target: MemoryUnit
    value: str
    sources: list[MemoryUnit] = field(default_factory=list)


@dataclass(slots=True)
class SchemaPropertyMergePlan:
    """Pure mutation plan produced for one extracted schema-property batch."""

    additions: list[MemoryUnit] = field(default_factory=list)
    updates: list[SchemaPropertyMergeUpdate] = field(default_factory=list)
    archives: list[MemoryUnit] = field(default_factory=list)
    archive_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SchemaPropertyMergeExecution:
    """IDs changed while applying a schema property merge plan."""

    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    archived_ids: list[str] = field(default_factory=list)


class SchemaPropertyMergePlanner:
    """Build MindMemOS-compatible add/update/archive decisions for schema properties."""

    def __init__(
        self,
        *,
        storage: Storage,
        embedder: Embedder,
        llm: LLM,
        top_k: int = 5,
        vector_candidate_multiplier: int = 4,
        kv_fallback_limit: int = 1000,
        merge_enabled: bool = True,
    ) -> None:
        if top_k < 1:
            raise ValueError("schema property merge top_k must be >= 1")
        if vector_candidate_multiplier < 1:
            raise ValueError("schema property merge vector_candidate_multiplier must be >= 1")
        if kv_fallback_limit < 1:
            raise ValueError("schema property merge kv_fallback_limit must be >= 1")
        self._storage = storage
        self._embedder = embedder
        self._llm = llm
        self._top_k = top_k
        self._vector_candidate_multiplier = vector_candidate_multiplier
        self._kv_fallback_limit = kv_fallback_limit
        self._merge_enabled = bool(merge_enabled)

    def plan(self, candidates: list[MemoryUnit]) -> SchemaPropertyMergePlan:
        """Plan mutations, grouping candidates by exact Scope and schema entity key."""

        plan = SchemaPropertyMergePlan()
        groups: dict[tuple[str, str, str, str, str, str], list[MemoryUnit]] = {}
        for candidate in candidates:
            if not _is_schema_candidate(candidate):
                plan.additions.append(candidate)
                continue
            entity_key = str(candidate.system_metadata.get("schema_entity_key") or "").strip()
            if not entity_key:
                if _is_delete_candidate(candidate):
                    logger.warning(
                        "SchemaPropertyMergePlanner: delete candidate %s has no "
                        "schema_entity_key; ignore safely",
                        candidate.id,
                    )
                    continue
                logger.warning(
                    "SchemaPropertyMergePlanner: candidate %s has no schema_entity_key; ADD",
                    candidate.id,
                )
                plan.additions.append(candidate)
                continue
            scope = candidate.scope
            group_key = (
                scope.org,
                scope.space,
                scope.user,
                scope.agent,
                scope.session,
                entity_key,
            )
            groups.setdefault(group_key, []).append(candidate)

        for group in groups.values():
            group_plan = self._plan_entity(group)
            plan.additions.extend(group_plan.additions)
            plan.updates.extend(group_plan.updates)
            plan.archives.extend(group_plan.archives)
            plan.archive_reasons.update(group_plan.archive_reasons)
        return plan

    def _plan_entity(self, candidates: list[MemoryUnit]) -> SchemaPropertyMergePlan:
        deletes = [candidate for candidate in candidates if _is_delete_candidate(candidate)]
        sets = [candidate for candidate in candidates if not _is_delete_candidate(candidate)]
        delete_plan = self._plan_explicit_deletes(deletes)
        set_plan = (
            self._plan_sets(sets)
            if self._merge_enabled
            else SchemaPropertyMergePlan(additions=list(sets))
        )
        return _combine_plans(delete_plan, set_plan)

    def _plan_sets(self, candidates: list[MemoryUnit]) -> SchemaPropertyMergePlan:
        if not candidates:
            return SchemaPropertyMergePlan()

        try:
            match_lists = self._find_matches(candidates)
        except Exception as exc:
            logger.warning(
                "SchemaPropertyMergePlanner: recall failed for entity %s; append all: %s",
                candidates[0].system_metadata.get("schema_entity_key"),
                exc,
            )
            return SchemaPropertyMergePlan(additions=list(candidates))

        direct: list[MemoryUnit] = []
        new_with_similar: list[MemoryUnit] = []
        existing_by_id: dict[str, MemoryUnit] = {}
        for candidate, matches in zip(candidates, match_lists, strict=True):
            if not matches:
                direct.append(candidate)
                continue
            new_with_similar.append(candidate)
            for memory, _score in matches:
                existing_by_id.setdefault(memory.id, memory)

        if not new_with_similar:
            return SchemaPropertyMergePlan(additions=direct)

        existing = list(existing_by_id.values())
        p_items = {f"p{index}": unit for index, unit in enumerate(existing, start=1)}
        n_items = {f"n{index}": unit for index, unit in enumerate(new_with_similar, start=1)}
        prompt = self._merge_prompt(candidates[0], p_items, n_items)
        try:
            response = self._llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0,
                max_tokens=4096,
            )
            decision = _parse_json_object(response)
        except Exception as exc:
            logger.warning(
                "SchemaPropertyMergePlanner: merge LLM failed; append all: %s",
                exc,
            )
            return SchemaPropertyMergePlan(additions=[*direct, *new_with_similar])

        return self._decision_to_plan(
            direct=direct,
            p_items=p_items,
            n_items=n_items,
            decision=decision,
        )

    def _plan_explicit_deletes(
        self,
        candidates: list[MemoryUnit],
    ) -> SchemaPropertyMergePlan:
        """Resolve explicit delete commands without ever persisting the command itself."""

        plan = SchemaPropertyMergePlan()
        if not candidates:
            return plan
        try:
            match_lists = self._find_matches(candidates)
        except Exception as exc:
            logger.warning(
                "SchemaPropertyMergePlanner: explicit delete recall failed; keep existing: %s",
                exc,
            )
            return plan

        archived_ids: set[str] = set()
        for candidate, matches in zip(candidates, match_lists, strict=True):
            eligible = [
                memory
                for memory, _score in matches
                if memory.id not in archived_ids and _delete_target_compatible(memory, candidate)
            ]
            if not eligible:
                continue

            exact = [memory for memory in eligible if _same_fact_text(memory, candidate)]
            selected = exact or self._select_delete_targets(candidate, eligible)
            for target in selected:
                if target.id in archived_ids:
                    continue
                archived_ids.add(target.id)
                plan.archives.append(target)
                plan.archive_reasons[target.id] = "schema_property_delete"
        return plan

    def _select_delete_targets(
        self,
        candidate: MemoryUnit,
        existing: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        p_items = {f"p{index}": unit for index, unit in enumerate(existing, start=1)}
        existing_text = "\n".join(
            f'{item_id}: time={_event_time_text(unit)}, value="{unit.content}"'
            for item_id, unit in p_items.items()
        )
        prompt = (
            PROPERTY_DELETE_DECISION_PROMPT.replace(
                "{entity_name}", str(candidate.system_metadata.get("schema_entity_name") or "")
            )
            .replace(
                "{entity_type}", str(candidate.system_metadata.get("schema_entity_type") or "")
            )
            .replace(
                "{property_name}",
                str(candidate.system_metadata.get("schema_property_name") or ""),
            )
            .replace("{delete_time}", _event_time_text(candidate))
            .replace("{delete_value}", candidate.content)
            .replace("{existing_properties}", existing_text)
        )
        try:
            response = self._llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0,
                max_tokens=1024,
            )
            decision = _parse_json_object(response)
        except Exception as exc:
            logger.warning(
                "SchemaPropertyMergePlanner: explicit delete LLM failed; keep existing: %s",
                exc,
            )
            return []
        archive_ids = decision.get("archive", [])
        if not isinstance(archive_ids, list):
            return []
        return [
            p_items[item_id]
            for item_id in dict.fromkeys(str(value) for value in archive_ids)
            if item_id in p_items
        ]

    def _find_matches(
        self,
        candidates: list[MemoryUnit],
    ) -> list[list[tuple[MemoryUnit, float]]]:
        texts = [_property_text(candidate) for candidate in candidates]
        vectors = self._embedder.embed(texts)
        if len(vectors) != len(candidates):
            raise ValueError("embedder returned a different number of property vectors")

        if self._storage.has_vector():
            return [
                self._vector_matches(candidate, vector)
                for candidate, vector in zip(candidates, vectors, strict=True)
            ]
        return self._kv_matches(candidates, vectors)

    def _vector_matches(
        self,
        candidate: MemoryUnit,
        vector: list[float],
    ) -> list[tuple[MemoryUnit, float]]:
        entity_key = str(candidate.system_metadata["schema_entity_key"])
        clauses = [
            FilterClause("system_metadata.extraction_mode", FilterOp.EQ, _SCHEMA_MODE),
            FilterClause("system_metadata.schema_entity_key", FilterOp.EQ, entity_key),
            FilterClause("lifecycle", FilterOp.EQ, LifecycleState.ACTIVE.value),
        ]
        if _is_delete_candidate(candidate):
            clauses.append(
                FilterClause(
                    "system_metadata.schema_property_name",
                    FilterOp.EQ,
                    str(candidate.system_metadata.get("schema_property_name") or ""),
                )
            )
        filters = FilterGroup(
            FilterLogic.AND,
            clauses,
        )
        hits = self._storage.vector.search(
            candidate.scope,
            VectorQuery(
                vector=vector,
                top_k=self._top_k * self._vector_candidate_multiplier,
                filters=filters,
                return_metadata=True,
            ),
        )
        unit_ids = _unit_ids_from_hits(hits)
        loaded = {unit.id: unit for unit in self._storage.get(candidate.scope, unit_ids)}
        scores: dict[str, float] = {}
        for hit in hits:
            unit_id = _unit_id_from_hit(hit)
            unit = loaded.get(unit_id)
            if unit is None or not _same_schema_entity(unit, candidate):
                continue
            if unit.lifecycle is not LifecycleState.ACTIVE:
                continue
            if _is_delete_candidate(candidate) and unit.system_metadata.get(
                "schema_property_name"
            ) != candidate.system_metadata.get("schema_property_name"):
                continue
            scores[unit_id] = max(scores.get(unit_id, -math.inf), float(hit.score))
        ranked = sorted(
            ((loaded[unit_id], score) for unit_id, score in scores.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[: self._top_k]

    def _kv_matches(
        self,
        candidates: list[MemoryUnit],
        candidate_vectors: list[list[float]],
    ) -> list[list[tuple[MemoryUnit, float]]]:
        first = candidates[0]
        entity_key = str(first.system_metadata["schema_entity_key"])
        filters = FilterGroup(
            FilterLogic.AND,
            [
                FilterClause("system_metadata.extraction_mode", FilterOp.EQ, _SCHEMA_MODE),
                FilterClause("system_metadata.schema_entity_key", FilterOp.EQ, entity_key),
                FilterClause("lifecycle", FilterOp.EQ, LifecycleState.ACTIVE.value),
            ],
        )
        page = self._storage.list(
            first.scope,
            limit=self._kv_fallback_limit,
            filters=filters,
        )
        existing = [unit for unit in page.items if _same_schema_entity(unit, first)]
        if page.count > self._kv_fallback_limit:
            logger.warning(
                "SchemaPropertyMergePlanner: KV fallback capped entity memories at %d of %d",
                self._kv_fallback_limit,
                page.count,
            )
        if not existing:
            return [[] for _ in candidates]
        existing_vectors = self._embedder.embed([_property_text(unit) for unit in existing])
        if len(existing_vectors) != len(existing):
            raise ValueError("embedder returned a different number of existing vectors")
        results: list[list[tuple[MemoryUnit, float]]] = []
        for candidate, candidate_vector in zip(
            candidates,
            candidate_vectors,
            strict=True,
        ):
            pool = existing
            if _is_delete_candidate(candidate):
                property_name = candidate.system_metadata.get("schema_property_name")
                pool = [
                    unit
                    for unit in existing
                    if unit.system_metadata.get("schema_property_name") == property_name
                ]
            pool_ids = {unit.id for unit in pool}
            scored = (
                (unit, _cosine(candidate_vector, existing_vector))
                for unit, existing_vector in zip(existing, existing_vectors, strict=True)
                if unit.id in pool_ids
            )
            ranked = sorted(
                (item for item in scored if item[1] > 0.0),
                key=lambda item: item[1],
                reverse=True,
            )
            results.append(ranked[: self._top_k])
        return results

    @staticmethod
    def _merge_prompt(
        representative: MemoryUnit,
        p_items: dict[str, MemoryUnit],
        n_items: dict[str, MemoryUnit],
    ) -> str:
        entity_name = representative.system_metadata.get("schema_entity_name", "")
        entity_type = representative.system_metadata.get("schema_entity_type", "")
        existing_text = "\n".join(
            f'{item_id}: [{unit.system_metadata.get("schema_property_name", "")}] '
            f'time={_event_time_text(unit)}, value="{unit.content}"'
            for item_id, unit in p_items.items()
        )
        new_text = "\n".join(
            f'{item_id}: [{unit.system_metadata.get("schema_property_name", "")}] '
            f'time={_event_time_text(unit)}, value="{unit.content}"'
            for item_id, unit in n_items.items()
        )
        return (
            _PROPERTY_MERGE_SYSTEM_PROMPT.replace("{entity_name}", str(entity_name))
            .replace("{entity_type}", str(entity_type))
            .replace("{existing_properties}", existing_text)
            .replace("{new_properties}", new_text)
        )

    @staticmethod
    def _decision_to_plan(
        *,
        direct: list[MemoryUnit],
        p_items: dict[str, MemoryUnit],
        n_items: dict[str, MemoryUnit],
        decision: dict[str, Any],
    ) -> SchemaPropertyMergePlan:
        plan = SchemaPropertyMergePlan(additions=list(direct))
        affected_targets: set[str] = set()
        consumed_new: set[str] = set()
        all_new_sources = list(n_items.values())

        existing_decisions = decision.get("existing", [])
        if not isinstance(existing_decisions, list):
            existing_decisions = []
        for item in existing_decisions:
            if not isinstance(item, dict):
                continue
            target = p_items.get(str(item.get("id") or ""))
            if target is None or target.id in affected_targets:
                continue
            op = str(item.get("op") or "").lower()
            if op == "delete":
                plan.archives.append(target)
                affected_targets.add(target.id)
            elif op == "update":
                value = str(item.get("value") or "").strip()
                sources = [
                    source for source in all_new_sources if _same_property_event(target, source)
                ]
                if value and sources:
                    plan.updates.append(
                        SchemaPropertyMergeUpdate(
                            target=target,
                            value=value,
                            sources=sources,
                        )
                    )
                    affected_targets.add(target.id)
                    consumed_new.update(
                        n_id for n_id, source in n_items.items() if source in sources
                    )

        new_decisions = decision.get("new", [])
        if not isinstance(new_decisions, list):
            new_decisions = []
        for item in new_decisions:
            if not isinstance(item, dict):
                continue
            n_id = str(item.get("id") or "")
            source = n_items.get(n_id)
            if source is None or n_id in consumed_new:
                continue
            op = str(item.get("op") or "").lower()
            if op == "delete":
                consumed_new.add(n_id)
                continue
            if op != "update":
                continue
            target = p_items.get(str(item.get("target") or ""))
            value = str(item.get("value") or "").strip()
            if (
                target is None
                or not value
                or target.id in affected_targets
                or not _same_property_event(target, source)
            ):
                continue
            plan.updates.append(
                SchemaPropertyMergeUpdate(target=target, value=value, sources=[source])
            )
            affected_targets.add(target.id)
            consumed_new.add(n_id)

        plan.additions.extend(unit for n_id, unit in n_items.items() if n_id not in consumed_new)
        return plan


class SchemaPropertyMergeExecutor:
    """Apply a planned schema-property mutation batch through Storage and IndexBuilder."""

    def __init__(self, *, storage: Storage, index_builder: IndexBuilder) -> None:
        self._storage = storage
        self._index = index_builder

    def apply(self, plan: SchemaPropertyMergePlan) -> SchemaPropertyMergeExecution:
        """Apply additions first, then updates and archives to prefer duplication over loss."""

        result = SchemaPropertyMergeExecution()
        for candidate in plan.additions:
            self._storage.add(candidate.scope, [candidate])
            self._index.build([candidate])
            result.created_ids.append(candidate.id)

        for operation in plan.updates:
            replacement = _replacement_property(operation)
            self._storage.add(replacement.scope, [replacement])
            self._index.build([replacement])
            superseded = copy.deepcopy(operation.target)
            superseded.lifecycle = LifecycleState.SUPERSEDED
            superseded.temporal.t_invalid = replacement.temporal.t_valid
            superseded.system_metadata["property_merge_action"] = "supersede"
            superseded.system_metadata["property_merge_replaced_by"] = replacement.id
            self._storage.update(superseded.scope, [superseded])
            self._index.update([superseded])
            result.created_ids.append(replacement.id)
            result.superseded_ids.append(superseded.id)

        now = datetime.now(timezone.utc)
        for target in plan.archives:
            archived = copy.deepcopy(target)
            archived.lifecycle = LifecycleState.ARCHIVED
            archived.temporal.t_invalid = now
            archived.system_metadata["property_merge_action"] = "archive"
            archived.system_metadata["property_merge_reason"] = plan.archive_reasons.get(
                target.id,
                "schema_property_merge",
            )
            self._storage.update(archived.scope, [archived])
            self._index.update([archived])
            result.archived_ids.append(archived.id)
        return result


def _replacement_property(operation: SchemaPropertyMergeUpdate) -> MemoryUnit:
    updated = copy.deepcopy(operation.sources[0])
    updated.supersedes = operation.target.id
    updated.lifecycle = LifecycleState.ACTIVE
    updated.temporal.t_valid = datetime.now(timezone.utc)
    updated.temporal.t_invalid = None
    updated.segments = [
        Segment(content=operation.value, assets=list(updated.assets), source=updated.source)
    ]
    provenance = _dedupe_strings(
        [
            updated.source_ref,
            *updated.provenance,
            operation.target.source_ref,
            *operation.target.provenance,
            *(
                source_id
                for unit in operation.sources
                for source_id in [unit.source_ref, *unit.provenance]
            ),
        ]
    )
    updated.provenance = provenance
    if provenance:
        updated.source_ref = provenance[0]
    source_temporal = operation.sources[0].temporal
    updated.temporal.t_event = source_temporal.t_event
    updated.temporal.t_message = _latest_message_time(operation.target, operation.sources)
    updated.tags = merge_unit_tags(
        updated.tags,
        [tag for unit in operation.sources for tag in unit.tags],
    )
    updated.system_metadata["schema_property_operation"] = "update"
    for legacy_key in (
        "schema_entity_record_time",
        "schema_property_time",
        "schema_source_unit_ids",
    ):
        updated.system_metadata.pop(legacy_key, None)
    updated.system_metadata["property_merge_action"] = "supersede"
    updated.system_metadata["property_merge_merged_from_memory_ids"] = _dedupe_strings(
        unit.id for unit in operation.sources
    )
    return updated


def _same_property_event(target: MemoryUnit, source: MemoryUnit) -> bool:
    """Only the same property at the same known event time may be corrected."""

    if target.system_metadata.get("schema_property_name") != source.system_metadata.get(
        "schema_property_name"
    ):
        return False
    target_event = target.temporal.t_event
    source_event = source.temporal.t_event
    if target_event is not None or source_event is not None:
        return (
            target_event is not None and source_event is not None and target_event == source_event
        )
    target_interval = _event_interval_key(target)
    source_interval = _event_interval_key(source)
    return bool(target_interval and target_interval == source_interval)


def _event_time_text(unit: MemoryUnit) -> str:
    if unit.temporal.t_event is not None:
        return unit.temporal.t_event.isoformat()
    return str(unit.system_metadata.get("schema_event_time") or "")


def _event_interval_key(unit: MemoryUnit) -> tuple[str, str, str] | None:
    metadata = unit.system_metadata
    precision = str(metadata.get("schema_event_time_precision") or "").strip()
    start = str(metadata.get("schema_event_time_start") or "").strip()
    end = str(metadata.get("schema_event_time_end") or "").strip()
    return (precision, start, end) if precision and start and end else None


def _merged_event_time(target: MemoryUnit, sources: list[MemoryUnit]) -> datetime | None:
    """Prefer one unambiguous new event time; otherwise retain an unchallenged old time."""

    source_values = {unit.temporal.t_event for unit in sources if unit.temporal.t_event is not None}
    if len(source_values) == 1:
        return next(iter(source_values))
    if len(source_values) > 1:
        return None
    return target.temporal.t_event


def _latest_message_time(target: MemoryUnit, sources: list[MemoryUnit]) -> datetime | None:
    values = [
        unit.temporal.t_message
        for unit in [target, *sources]
        if unit.temporal.t_message is not None
    ]
    return max(values) if values else None


def _property_text(unit: MemoryUnit) -> str:
    name = str(unit.system_metadata.get("schema_property_name") or "")
    return f"{name}{_PROPERTY_TEXT_SEPARATOR}{unit.content}"


def _is_schema_candidate(unit: MemoryUnit) -> bool:
    return unit.system_metadata.get("extraction_mode") == _SCHEMA_MODE


def _is_delete_candidate(unit: MemoryUnit) -> bool:
    return str(unit.system_metadata.get("schema_property_operation") or "set").lower() == "delete"


def _delete_target_compatible(target: MemoryUnit, command: MemoryUnit) -> bool:
    if target.system_metadata.get("schema_property_name") != command.system_metadata.get(
        "schema_property_name"
    ):
        return False
    if command.temporal.t_event is None and _event_interval_key(command) is None:
        return True
    return _same_property_event(target, command)


def _same_fact_text(target: MemoryUnit, command: MemoryUnit) -> bool:
    return _normalized_fact_text(target.content) == _normalized_fact_text(command.content)


def _normalized_fact_text(value: object) -> str:
    return " ".join(str(value).casefold().split())


def _combine_plans(
    delete_plan: SchemaPropertyMergePlan,
    set_plan: SchemaPropertyMergePlan,
) -> SchemaPropertyMergePlan:
    """Combine explicit deletion with ordinary merge; explicit delete wins target conflicts."""

    deleted_ids = {unit.id for unit in delete_plan.archives}
    additions = [*delete_plan.additions, *set_plan.additions]
    updates: list[SchemaPropertyMergeUpdate] = []
    for operation in set_plan.updates:
        if operation.target.id in deleted_ids:
            additions.extend(operation.sources)
        else:
            updates.append(operation)
    archives = _dedupe_units([*delete_plan.archives, *set_plan.archives])
    return SchemaPropertyMergePlan(
        additions=_dedupe_units(additions),
        updates=updates,
        archives=archives,
        archive_reasons={
            **set_plan.archive_reasons,
            **delete_plan.archive_reasons,
        },
    )


def _dedupe_units(units: list[MemoryUnit]) -> list[MemoryUnit]:
    return list({unit.id: unit for unit in units}.values())


def _same_schema_entity(existing: MemoryUnit, candidate: MemoryUnit) -> bool:
    return (
        existing.system_metadata.get("extraction_mode") == _SCHEMA_MODE
        and existing.system_metadata.get("schema_entity_key")
        == candidate.system_metadata.get("schema_entity_key")
        and existing.scope == candidate.scope
    )


def _unit_ids_from_hits(hits: list[ScoredID]) -> list[str]:
    return _dedupe_strings([_unit_id_from_hit(hit) for hit in hits])


def _unit_id_from_hit(hit: ScoredID) -> str:
    if isinstance(hit.metadata, dict) and hit.metadata.get("unit_id"):
        return str(hit.metadata["unit_id"])
    match = re.match(r"^(.*)-(?:layer-l[01]|\d+)$", hit.id)
    return match.group(1) if match else hit.id


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _parse_json_object(response: str) -> dict[str, Any]:
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("schema property merge response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("schema property merge response must be a JSON object")
    return value


def _dedupe_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
