"""Conservative batch merge for canonical Schema Property MemoryUnits."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit, Segment
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.index_builder import IndexBuilder
from jiuwen_memory.construction.schema_prompts import (
    AGENT_MEMORY_PROPERTY_MERGE_APPENDIX,
    PROPERTY_DELETE_DECISION_PROMPT,
    PROPERTY_MERGE_DECISION_PROMPT,
)
from jiuwen_memory.storage._schema_property_index import SchemaPropertyIndex
from jiuwen_memory.storage.kv import KVStore, load_units
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode

logger = get_logger(__name__)

_MERGE_PROMPT = PROPERTY_MERGE_DECISION_PROMPT + AGENT_MEMORY_PROPERTY_MERGE_APPENDIX
_DELETE_PROMPT = PROPERTY_DELETE_DECISION_PROMPT


@dataclass(slots=True)
class SchemaPropertyMergeUpdate:
    """One replacement of an old property by a merged new version."""

    target: MemoryUnit
    value: str
    sources: list[MemoryUnit] = field(default_factory=list)


@dataclass(slots=True)
class SchemaPropertyMergePlan:
    """Side-effect-free property mutation plan."""

    additions: list[MemoryUnit] = field(default_factory=list)
    updates: list[SchemaPropertyMergeUpdate] = field(default_factory=list)
    archives: list[MemoryUnit] = field(default_factory=list)
    archive_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SchemaPropertyMergeExecution:
    """IDs changed by one executed plan."""

    created_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    archived_ids: list[str] = field(default_factory=list)


class SchemaPropertyMergePlanner:
    """Recall same-entity properties and ask the LLM for conservative batch changes."""

    def __init__(
        self,
        *,
        kv: KVStore,
        embedder: Embedder,
        llm: LLM,
        top_k: int = 5,
        fallback_limit: int = 1000,
        merge_enabled: bool = False,
    ) -> None:
        self._kv = kv
        self._property_index = SchemaPropertyIndex(kv)
        self._embedder = embedder
        self._llm = llm
        self._top_k = max(1, top_k)
        self._fallback_limit = max(1, fallback_limit)
        self._merge_enabled = merge_enabled

    def plan(self, candidates: list[MemoryUnit]) -> SchemaPropertyMergePlan:
        """Plan per canonical entity; delete commands are never persisted."""

        result = SchemaPropertyMergePlan()
        groups: dict[tuple[str, ...], list[MemoryUnit]] = {}
        for candidate in candidates:
            if candidate.system_metadata.get("extraction_mode") != "schema":
                result.additions.append(candidate)
                continue
            scope = candidate.scope
            key = (
                scope.org,
                scope.space,
                scope.user,
                scope.agent,
                scope.session,
                str(candidate.system_metadata.get("schema_entity_key") or ""),
            )
            groups.setdefault(key, []).append(candidate)
        for group in groups.values():
            self._plan_group(group, result)
        return result

    def _plan_group(
        self,
        candidates: list[MemoryUnit],
        result: SchemaPropertyMergePlan,
    ) -> None:
        deletes = [candidate for candidate in candidates if _is_delete(candidate)]
        additions = [candidate for candidate in candidates if not _is_delete(candidate)]
        if not deletes and not self._merge_enabled:
            result.additions.extend(additions)
            return
        existing = self._existing(candidates[0])
        self._plan_deletes(deletes, existing, result)
        if not self._merge_enabled or not additions or not existing:
            result.additions.extend(additions)
            return
        self._plan_sets(additions, existing, result)

    def _existing(self, candidate: MemoryUnit) -> list[MemoryUnit]:
        entity_key = str(candidate.system_metadata.get("schema_entity_key") or "")
        lookup = self._property_index.lookup(candidate.scope, entity_key)
        if lookup.indexed:
            return [
                unit
                for unit in load_units(self._kv, candidate.scope, lookup.unit_ids)
                if unit.lifecycle is LifecycleState.ACTIVE
            ]
        result: list[MemoryUnit] = []
        for _key, raw in self._kv.scan(candidate.scope, "/memory/"):
            unit = loads(raw)
            if unit is None or unit.lifecycle is not LifecycleState.ACTIVE:
                continue
            metadata = unit.system_metadata
            if metadata.get("extraction_mode") != "schema":
                continue
            if str(metadata.get("schema_entity_key") or "") == entity_key:
                result.append(unit)
                if len(result) >= self._fallback_limit:
                    break
        return result

    def _plan_sets(
        self,
        candidates: list[MemoryUnit],
        existing: list[MemoryUnit],
        result: SchemaPropertyMergePlan,
    ) -> None:
        try:
            selected = self._rank_matches(candidates, existing)
            if not selected:
                result.additions.extend(candidates)
                return
            p_items = {f"p{index}": unit for index, unit in enumerate(selected, start=1)}
            n_items = {f"n{index}": unit for index, unit in enumerate(candidates, start=1)}
            response = self._llm.chat(
                [ChatMessage(role="user", content=_merge_prompt(candidates[0], p_items, n_items))],
                temperature=0,
                max_tokens=2048,
            )
            decision = _parse_json(response)
            _apply_decision(result, p_items, n_items, decision)
        except Exception as exc:
            logger.warning("SchemaPropertyMergePlanner: merge failed; append all: %s", exc)
            result.additions.extend(candidates)

    def _plan_deletes(
        self,
        commands: list[MemoryUnit],
        existing: list[MemoryUnit],
        result: SchemaPropertyMergePlan,
    ) -> None:
        archived: set[str] = set()
        for command in commands:
            candidates = [
                unit
                for unit in existing
                if unit.id not in archived and _delete_compatible(unit, command)
            ]
            exact = [unit for unit in candidates if _fact(unit) == _fact(command)]
            selected = exact or self._llm_delete_targets(command, candidates)
            for target in selected:
                if target.id in archived:
                    continue
                archived.add(target.id)
                result.archives.append(target)
                result.archive_reasons[target.id] = "schema_property_delete"

    def _llm_delete_targets(
        self,
        command: MemoryUnit,
        candidates: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        if not candidates:
            return []
        p_items = {f"p{index}": unit for index, unit in enumerate(candidates, start=1)}
        prompt = _DELETE_PROMPT.format(
            entity_name=command.system_metadata.get("schema_entity_name", ""),
            entity_type=command.system_metadata.get("schema_entity_type", ""),
            property_name=command.system_metadata.get("schema_property_name", ""),
            delete_time=_event_key(command) or "unknown",
            delete_value=command.content,
            existing_properties=_format_items(p_items),
        )
        try:
            response = self._llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0,
                max_tokens=512,
            )
            archive = _parse_json(response).get("archive", [])
        except Exception as exc:
            logger.warning("SchemaPropertyMergePlanner: delete decision failed: %s", exc)
            return []
        if not isinstance(archive, list):
            return []
        return [p_items[item_id] for item_id in map(str, archive) if item_id in p_items]

    def _rank_matches(
        self,
        candidates: list[MemoryUnit],
        existing: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        texts = [_property_text(unit) for unit in [*candidates, *existing]]
        vectors = self._embedder.embed(texts)
        candidate_vectors = vectors[: len(candidates)]
        existing_vectors = vectors[len(candidates) :]
        scores: dict[str, float] = {}
        for candidate_vector in candidate_vectors:
            for unit, vector in zip(existing, existing_vectors, strict=True):
                score = _cosine(candidate_vector, vector)
                scores[unit.id] = max(scores.get(unit.id, -math.inf), score)
        ranked = sorted(existing, key=lambda unit: scores.get(unit.id, -math.inf), reverse=True)
        return ranked[: self._top_k]


class SchemaPropertyMergeExecutor:
    """Apply a plan exclusively through the current IndexBuilder write boundary."""

    def __init__(self, index_builder: IndexBuilder) -> None:
        self._index = index_builder

    def apply(self, plan: SchemaPropertyMergePlan) -> SchemaPropertyMergeExecution:
        """Write additions before retiring old versions, preferring duplicates over loss."""

        result = SchemaPropertyMergeExecution()
        if plan.additions:
            self._index.build(plan.additions, mode=IndexWriteMode.ALL)
            result.created_ids.extend(unit.id for unit in plan.additions)
        for operation in plan.updates:
            replacement = _replacement(operation)
            self._index.build([replacement], mode=IndexWriteMode.ALL)
            superseded = _retired(operation.target, LifecycleState.SUPERSEDED)
            superseded.system_metadata["property_merge_replaced_by"] = replacement.id
            self._index.update([superseded], mode=IndexWriteMode.FORWARD_ONLY)
            self._index.remove([superseded], mode=IndexRemoveMode.SOFT)
            result.created_ids.append(replacement.id)
            result.superseded_ids.append(superseded.id)
        for target in plan.archives:
            archived = _retired(target, LifecycleState.ARCHIVED)
            archived.system_metadata["property_merge_reason"] = plan.archive_reasons.get(
                target.id,
                "schema_property_merge",
            )
            self._index.update([archived], mode=IndexWriteMode.FORWARD_ONLY)
            self._index.remove([archived], mode=IndexRemoveMode.SOFT)
            result.archived_ids.append(archived.id)
        return result


def _apply_decision(
    plan: SchemaPropertyMergePlan,
    p_items: dict[str, MemoryUnit],
    n_items: dict[str, MemoryUnit],
    decision: dict[str, object],
) -> None:
    affected: set[str] = set()
    consumed: set[str] = set()
    existing_decisions = decision.get("existing", [])
    if not isinstance(existing_decisions, list):
        existing_decisions = []
    for item in existing_decisions:
        if not isinstance(item, dict):
            continue
        target = p_items.get(str(item.get("id") or ""))
        if target is None or target.id in affected:
            continue
        operation = str(item.get("op") or "").lower()
        if operation == "delete":
            plan.archives.append(target)
            affected.add(target.id)
            continue
        value = str(item.get("value") or "").strip()
        sources = [source for source in n_items.values() if _same_event(target, source)]
        if operation == "update" and value and sources:
            plan.updates.append(SchemaPropertyMergeUpdate(target, value, sources))
            affected.add(target.id)
            consumed.update(key for key, source in n_items.items() if source in sources)
    new_decisions = decision.get("new", [])
    if not isinstance(new_decisions, list):
        new_decisions = []
    for item in new_decisions:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        source = n_items.get(item_id)
        operation = str(item.get("op") or "").lower()
        if source is None or item_id in consumed:
            continue
        if operation == "delete":
            consumed.add(item_id)
            continue
        target = p_items.get(str(item.get("target") or ""))
        value = str(item.get("value") or "").strip()
        if (
            operation == "update"
            and target is not None
            and target.id not in affected
            and value
            and _same_event(target, source)
        ):
            plan.updates.append(SchemaPropertyMergeUpdate(target, value, [source]))
            affected.add(target.id)
            consumed.add(item_id)
    plan.additions.extend(unit for item_id, unit in n_items.items() if item_id not in consumed)


def _replacement(operation: SchemaPropertyMergeUpdate) -> MemoryUnit:
    replacement = copy.deepcopy(operation.sources[0])
    replacement.supersedes = operation.target.id
    replacement.lifecycle = LifecycleState.ACTIVE
    replacement.segments = [
        Segment(content=operation.value, assets=list(replacement.assets), source=replacement.source)
    ]
    replacement.provenance = _dedupe(
        [
            replacement.source_ref,
            *replacement.provenance,
            operation.target.source_ref,
            *operation.target.provenance,
            *(
                source_id
                for source in operation.sources
                for source_id in [source.source_ref, *source.provenance]
            ),
        ]
    )
    if replacement.provenance:
        replacement.source_ref = replacement.provenance[0]
    replacement.tags = merge_unit_tags(
        replacement.tags,
        [tag for source in operation.sources for tag in source.tags],
    )
    replacement.temporal.t_valid = datetime.now(timezone.utc)
    replacement.temporal.t_invalid = None
    replacement.temporal.t_message = _latest_message_time(operation.target, operation.sources)
    replacement.system_metadata["schema_property_operation"] = "update"
    replacement.system_metadata["property_merge_action"] = "supersede"
    replacement.system_metadata["property_merge_merged_from_memory_ids"] = [
        source.id for source in operation.sources
    ]
    return replacement


def _retired(unit: MemoryUnit, lifecycle: LifecycleState) -> MemoryUnit:
    result = copy.deepcopy(unit)
    result.lifecycle = lifecycle
    result.temporal.t_invalid = datetime.now(timezone.utc)
    result.system_metadata["property_merge_action"] = lifecycle.value
    return result


def _merge_prompt(
    representative: MemoryUnit,
    existing: dict[str, MemoryUnit],
    new: dict[str, MemoryUnit],
) -> str:
    metadata = representative.system_metadata
    return _MERGE_PROMPT.format(
        entity_name=metadata.get("schema_entity_name", ""),
        entity_type=metadata.get("schema_entity_type", ""),
        existing_properties=_format_items(existing),
        new_properties=_format_items(new),
    )


def _format_items(items: dict[str, MemoryUnit]) -> str:
    return "\n".join(
        f"{item_id}: property={unit.system_metadata.get('schema_property_name', '')}; "
        f"time={_event_key(unit) or 'unknown'}; value={unit.content}"
        for item_id, unit in items.items()
    )


def _is_delete(unit: MemoryUnit) -> bool:
    operation = str(unit.system_metadata.get("schema_property_operation") or "set")
    return operation.casefold() == "delete"


def _delete_compatible(target: MemoryUnit, command: MemoryUnit) -> bool:
    if target.system_metadata.get("schema_property_name") != command.system_metadata.get(
        "schema_property_name"
    ):
        return False
    command_time = _event_key(command)
    return not command_time or command_time == _event_key(target)


def _same_event(target: MemoryUnit, source: MemoryUnit) -> bool:
    if target.system_metadata.get("schema_property_name") != source.system_metadata.get(
        "schema_property_name"
    ):
        return False
    target_time = _event_key(target)
    source_time = _event_key(source)
    return bool(target_time and source_time and target_time == source_time)


def _event_key(unit: MemoryUnit) -> str:
    if unit.temporal.t_event is not None:
        return unit.temporal.t_event.isoformat()
    metadata = unit.system_metadata
    precision = str(metadata.get("schema_event_precision") or "")
    start = str(metadata.get("schema_event_start") or "")
    end = str(metadata.get("schema_event_end") or "")
    return f"{precision}:{start}:{end}" if precision and start and end else ""


def _latest_message_time(target: MemoryUnit, sources: list[MemoryUnit]) -> datetime | None:
    values = [
        unit.temporal.t_message
        for unit in [target, *sources]
        if unit.temporal.t_message is not None
    ]
    return max(values) if values else None


def _property_text(unit: MemoryUnit) -> str:
    return f"{unit.system_metadata.get('schema_property_name', '')}: {unit.content}"


def _fact(unit: MemoryUnit) -> str:
    return " ".join(unit.content.casefold().split())


def _parse_json(value: str) -> dict[str, object]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("property merge response must be an object")
    return parsed


def _dedupe(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _cosine(first: list[float], second: list[float]) -> float:
    numerator = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    return numerator / (first_norm * second_norm) if first_norm and second_norm else 0.0


__all__ = [
    "SchemaPropertyMergeExecution",
    "SchemaPropertyMergeExecutor",
    "SchemaPropertyMergePlan",
    "SchemaPropertyMergePlanner",
]
