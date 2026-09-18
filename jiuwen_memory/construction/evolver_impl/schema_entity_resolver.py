"""Resolve provisional Schema entity observations to stable canonical identities."""

from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from jiuwen_memory.common.embedder.base import Embedder
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import LifecycleState, MemoryUnit
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import SchemaEntityRegistry
from jiuwen_memory.construction.schema_prompts import (
    AGENT_MEMORY_ENTITY_MERGE_APPENDIX,
    SINGLE_ENTITY_MERGE_PROMPT,
)
from jiuwen_memory.storage.kv import KVStore

logger = get_logger(__name__)

_GENERIC_NAMES = {"user", "assistant", "speaker", "participant", "用户", "助手", "说话者"}
_MERGE_PROMPT = SINGLE_ENTITY_MERGE_PROMPT + AGENT_MEMORY_ENTITY_MERGE_APPENDIX


@dataclass(slots=True)
class _Observation:
    units: list[MemoryUnit]
    name: str
    normalized_name: str
    entity_type: str
    schema_name: str
    aliases: list[str]
    description: str
    identity_kind: str = ""


@dataclass(slots=True)
class _EntityView:
    entity_id: str
    name: str
    normalized_name: str
    entity_type: str
    schema_name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    identity_kind: str = ""


class SchemaEntityResolver:
    """Apply exact-name, alias, semantic and LLM CREATE/UPDATE resolution."""

    def __init__(
        self,
        *,
        kv: KVStore,
        registry: SchemaEntityRegistry,
        embedder: Embedder,
        llm: LLM,
        enable_merge_decision: bool = True,
        recall_top_k: int = 15,
        max_merge_retries: int = 8,
        fallback_limit: int = 1000,
    ) -> None:
        self._kv = kv
        self._registry = registry
        self._embedder = embedder
        self._llm = llm
        self._enable_merge_decision = enable_merge_decision
        self._recall_top_k = max(1, recall_top_k)
        self._max_merge_retries = max(0, max_merge_retries)
        self._fallback_limit = max(1, fallback_limit)

    def resolve(self, candidates: list[MemoryUnit]) -> list[MemoryUnit]:
        """Resolve groups in-place; the caller persists facts before syncing the registry."""

        cache: dict[tuple[str, ...], list[_EntityView]] = {}
        for observation in _group_observations(candidates):
            scope = observation.units[0].scope
            scope_key = (
                scope.org,
                scope.space,
                scope.user,
                scope.agent,
                scope.session,
                observation.schema_name,
            )
            existing = cache.get(scope_key)
            if existing is None:
                existing = self._load_existing(observation.units[0], observation.schema_name)
                cache[scope_key] = existing
            resolved, action = self._resolve_one(observation, existing)
            _apply(observation, resolved, action)
            existing[:] = [item for item in existing if item.entity_id != resolved.entity_id]
            existing.append(_merge_view(resolved, observation))
        return candidates

    def sync(self, resolved: list[MemoryUnit]) -> list[MemoryUnit]:
        """Persist derived canonical records after Property mutations succeed."""

        return self._registry.sync(resolved)

    def _resolve_one(
        self,
        observation: _Observation,
        existing: list[_EntityView],
    ) -> tuple[_EntityView, str]:
        compatible = [item for item in existing if _compatible(observation, item)]
        exact = _exact(observation, compatible)
        if exact is not None:
            return exact, "exact"
        recalled = self._semantic_candidates(observation, compatible)
        selected = self._llm_target(observation, recalled)
        if selected is not None:
            return selected, "update"
        exact = _exact(observation, existing)
        if exact is not None:
            return exact, "exact"
        return (
            _EntityView(
                entity_id=str(uuid.uuid4()),
                name=observation.name,
                normalized_name=observation.normalized_name,
                entity_type=observation.entity_type,
                schema_name=observation.schema_name,
                aliases=list(observation.aliases),
                description=observation.description,
                identity_kind=observation.identity_kind,
            ),
            "create",
        )

    def _semantic_candidates(
        self,
        observation: _Observation,
        existing: list[_EntityView],
    ) -> list[_EntityView]:
        if not existing:
            return []
        texts = [_view_text(item) for item in existing]
        try:
            vectors = self._embedder.embed([_observation_text(observation), *texts])
        except Exception as exc:
            logger.warning("SchemaEntityResolver: semantic recall failed: %s", exc)
            return existing[: self._recall_top_k]
        query = vectors[0]
        ranked = sorted(
            zip(existing, vectors[1:], strict=True),
            key=lambda item: _cosine(query, item[1]),
            reverse=True,
        )
        return [item for item, _vector in ranked[: self._recall_top_k]]

    def _llm_target(
        self,
        observation: _Observation,
        candidates: list[_EntityView],
    ) -> _EntityView | None:
        if not self._enable_merge_decision or not candidates:
            return None
        by_id = {item.entity_id: item for item in candidates}
        by_name = {item.name: item for item in candidates}
        lines = "\n".join(
            f"- id={item.entity_id}; name={item.name}; type={item.entity_type}; "
            f"aliases={item.aliases}; description={item.description}"
            for item in candidates
        )
        prompt = _MERGE_PROMPT.format(
            entity_name=observation.name,
            entity_type=observation.entity_type,
            entity_description=(
                f"{observation.description}; aliases={observation.aliases}"
            ),
            existing_entities=lines,
        )
        for attempt in range(self._max_merge_retries):
            try:
                response = self._llm.chat(
                    [ChatMessage(role="user", content=prompt)],
                    temperature=0,
                    max_tokens=512,
                )
                decision = _parse_json(response)
                if str(decision.get("action") or "").lower() != "update":
                    return None
                target = by_name.get(str(decision.get("target_entity") or ""))
                if target is None:
                    target_name = _fuzzy_candidate_name(
                        str(decision.get("target_entity") or ""),
                        by_name,
                    )
                    target = by_name.get(target_name or "")
                if target is None:
                    target = by_id.get(str(decision.get("target_entity_id") or ""))
                if target is not None:
                    return target
            except Exception as exc:
                logger.warning(
                    "SchemaEntityResolver: merge decision attempt %d failed: %s",
                    attempt + 1,
                    exc,
                )
        return None

    def _load_existing(self, representative: MemoryUnit, schema_name: str) -> list[_EntityView]:
        registry = [
            _view_from_unit(unit) for unit in self._registry.list(
                representative.scope,
                schema_name,
                limit=self._fallback_limit,
            )
        ]
        if registry:
            return registry
        grouped: dict[str, list[MemoryUnit]] = {}
        matched = 0
        for _key, raw in self._kv.scan(representative.scope, "/memory/"):
            unit = loads(raw)
            if unit is None or unit.lifecycle is not LifecycleState.ACTIVE:
                continue
            metadata = unit.system_metadata
            if metadata.get("extraction_mode") != "schema":
                continue
            if str(metadata.get("schema_name") or "") != schema_name:
                continue
            entity_id = str(
                metadata.get("schema_entity_id") or metadata.get("schema_entity_key") or ""
            ).strip()
            if entity_id:
                grouped.setdefault(entity_id, []).append(unit)
                matched += 1
                if matched >= self._fallback_limit:
                    break
        result: list[_EntityView] = []
        for entity_id, units in grouped.items():
            names = {
                _normalize(str(unit.system_metadata.get("schema_entity_name") or ""))
                for unit in units
            }
            names.discard("")
            if len(names) > 1:
                logger.warning(
                    "SchemaEntityResolver: skip contaminated legacy entity %s with names=%s",
                    entity_id,
                    sorted(names),
                )
                continue
            result.append(_view_from_units(entity_id, units))
        return result


def _group_observations(candidates: list[MemoryUnit]) -> list[_Observation]:
    groups: dict[tuple[str, ...], list[MemoryUnit]] = {}
    order: list[tuple[str, ...]] = []
    for unit in candidates:
        metadata = unit.system_metadata
        if metadata.get("extraction_mode") != "schema":
            continue
        scope = unit.scope
        provisional = str(metadata.get("schema_entity_key") or "")
        key = (
            scope.org,
            scope.space,
            scope.user,
            scope.agent,
            scope.session,
            str(metadata.get("schema_name") or ""),
            str(metadata.get("schema_entity_type") or ""),
            provisional,
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(unit)
    return [_observation(groups[key]) for key in order]


def _observation(units: list[MemoryUnit]) -> _Observation:
    metadata = units[0].system_metadata
    name = str(metadata.get("schema_entity_name") or "").strip()
    aliases = _dedupe(
        alias
        for unit in units
        for alias in _strings(unit.system_metadata.get("schema_entity_aliases"))
    )
    descriptions = _dedupe(
        str(unit.system_metadata.get("schema_entity_description") or "") for unit in units
    )
    return _Observation(
        units=units,
        name=name,
        normalized_name=_normalize(name),
        entity_type=str(metadata.get("schema_entity_type") or ""),
        schema_name=str(metadata.get("schema_name") or ""),
        aliases=aliases,
        description=" ".join(descriptions[:5]),
        identity_kind=str(metadata.get("schema_entity_identity_kind") or ""),
    )


def _view_from_unit(unit: MemoryUnit) -> _EntityView:
    metadata = unit.system_metadata
    name = str(metadata.get("schema_entity_name") or "").strip()
    return _EntityView(
        entity_id=str(
            metadata.get("schema_entity_id") or metadata.get("schema_entity_key") or unit.id
        ),
        name=name,
        normalized_name=str(metadata.get("schema_entity_normalized_name") or _normalize(name)),
        entity_type=str(metadata.get("schema_entity_type") or ""),
        schema_name=str(metadata.get("schema_name") or ""),
        aliases=_strings(metadata.get("schema_entity_aliases")),
        description=str(metadata.get("schema_entity_description") or unit.content),
        identity_kind=str(metadata.get("schema_entity_identity_kind") or ""),
    )


def _view_from_units(entity_id: str, units: list[MemoryUnit]) -> _EntityView:
    representative = units[0]
    result = _view_from_unit(representative)
    result.entity_id = entity_id
    result.aliases = _dedupe(
        alias
        for unit in units
        for alias in _strings(unit.system_metadata.get("schema_entity_aliases"))
    )
    result.description = " ".join(
        _dedupe(
            str(unit.system_metadata.get("schema_entity_description") or unit.content)
            for unit in units
        )[:5]
    )
    return result


def _exact(observation: _Observation, candidates: list[_EntityView]) -> _EntityView | None:
    observation_names = {_normalize(observation.name), *map(_normalize, observation.aliases)}
    for candidate in candidates:
        candidate_names = {_normalize(candidate.name), *map(_normalize, candidate.aliases)}
        if observation.entity_type == candidate.entity_type and observation_names & candidate_names:
            return candidate
    return None


def _compatible(observation: _Observation, candidate: _EntityView) -> bool:
    if observation.entity_type != candidate.entity_type:
        return False
    observation_generic = _normalize(observation.name) in _GENERIC_NAMES
    candidate_generic = _normalize(candidate.name) in _GENERIC_NAMES
    if observation_generic != candidate_generic:
        return False
    if observation.identity_kind == candidate.identity_kind == "explicit_speaker":
        return _normalize(observation.name) == _normalize(candidate.name)
    return True


def _apply(observation: _Observation, resolved: _EntityView, action: str) -> None:
    aliases = _dedupe([*resolved.aliases, observation.name, *observation.aliases])
    for unit in observation.units:
        metadata = unit.system_metadata
        metadata["schema_entity_id"] = resolved.entity_id
        metadata["schema_entity_key"] = resolved.entity_id
        metadata["schema_entity_name"] = resolved.name
        metadata["schema_entity_normalized_name"] = resolved.normalized_name
        metadata["schema_entity_aliases"] = aliases
        metadata["schema_entity_observed_name"] = observation.name
        metadata["schema_entity_resolution"] = action


def _merge_view(view: _EntityView, observation: _Observation) -> _EntityView:
    return _EntityView(
        entity_id=view.entity_id,
        name=view.name,
        normalized_name=view.normalized_name,
        entity_type=view.entity_type,
        schema_name=view.schema_name,
        aliases=_dedupe([*view.aliases, observation.name, *observation.aliases]),
        description=view.description or observation.description,
        identity_kind=view.identity_kind or observation.identity_kind,
    )


def _parse_json(value: str) -> dict[str, object]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("entity merge response must be an object")
    return parsed


def _observation_text(value: _Observation) -> str:
    return f"{value.name}; {value.entity_type}; {'; '.join(value.aliases)}; {value.description}"


def _view_text(value: _EntityView) -> str:
    return f"{value.name}; {value.entity_type}; {'; '.join(value.aliases)}; {value.description}"


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip()
    text = re.sub(r"[（(].*$", "", text).strip()
    return " ".join(text.split()).casefold()


def _fuzzy_candidate_name(
    target_name: str,
    candidates: dict[str, _EntityView],
) -> str | None:
    target = target_name.casefold().strip()
    for name in candidates:
        if name.casefold() == target:
            return name
    for name in candidates:
        candidate = name.casefold()
        if target and (target in candidate or candidate in target):
            return name
    return None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dedupe(values) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _cosine(first: list[float], second: list[float]) -> float:
    numerator = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    return numerator / (first_norm * second_norm) if first_norm and second_norm else 0.0


__all__ = ["SchemaEntityResolver"]

