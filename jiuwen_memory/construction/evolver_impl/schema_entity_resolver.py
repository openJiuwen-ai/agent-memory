"""Resolve extracted schema entity observations to stable entity identities for Evolver.

The resolver mirrors MindMemOS' entity-resolution order while adapting its
independent Entity Store to agent-memory's MemoryUnit-only storage model:

1. build one entity observation from the extracted property MemoryUnits;
2. recall semantically similar existing entities;
3. reuse an exact base-name + entity-type match without an LLM call;
4. otherwise ask the LLM to choose CREATE or UPDATE;
5. reuse the selected entity id, or allocate a fresh UUID for CREATE.

Existing entity views are read from the hidden Schema Entity Registry and its optional
``schema_entities`` vector port. Persisted schema-property MemoryUnits remain a migration and
rebuild fallback. Neither graph projection nor temporal retrieval is a dependency. The resolved
id is written to both id/key fields for Property Merge.
"""

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
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    LifecycleState,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.common.type_def.chat import ChatMessage
from jiuwen_memory.common.type_def.memory_codec import loads
from jiuwen_memory.construction.evolver_impl.schema_entity_registry import (
    SCHEMA_ENTITY_KEY_PREFIX,
    schema_entity_key,
)
from jiuwen_memory.construction.schema_prompts import (
    AGENT_MEMORY_ENTITY_MERGE_APPENDIX,
    SINGLE_ENTITY_MERGE_PROMPT,
)
from jiuwen_memory.storage.storage import Storage
from jiuwen_memory.storage.types import VectorQuery

logger = get_logger(__name__)

_SCHEMA_MODE = "schema"
_NAME_SUFFIX_RE = re.compile(r"[\uFF08(].*$")

_ENTITY_MERGE_PROMPT = SINGLE_ENTITY_MERGE_PROMPT + AGENT_MEMORY_ENTITY_MERGE_APPENDIX


@dataclass
class _EntityObservation:
    units: list[MemoryUnit]
    name: str
    entity_type: str
    schema_name: str
    aliases: list[str]
    description: str
    identity_kind: str = ""


@dataclass
class _EntityView:
    entity_id: str
    entity_name: str
    entity_type: str
    description: str
    aliases: list[str] = field(default_factory=list)
    identity_kind: str = ""


class SchemaEntityResolver:
    """Resolve schema observations using MindMemOS-compatible CREATE/UPDATE semantics."""

    def __init__(
        self,
        *,
        storage: Storage,
        embedder: Embedder,
        llm: LLM,
        enable_merge_decision: bool = True,
        recall_top_k: int = 15,
        max_merge_retries: int = 8,
        kv_fallback_limit: int = 1000,
    ) -> None:
        if recall_top_k < 1:
            raise ValueError("schema entity recall_top_k must be >= 1")
        if max_merge_retries < 0:
            raise ValueError("schema entity max_merge_retries must be >= 0")
        if kv_fallback_limit < 1:
            raise ValueError("schema entity kv_fallback_limit must be >= 1")
        self._storage = storage
        self._embedder = embedder
        self._llm = llm
        self._enable_merge_decision = enable_merge_decision
        self._recall_top_k = recall_top_k
        self._max_merge_retries = max_merge_retries
        self._kv_fallback_limit = kv_fallback_limit

    def resolve(self, candidates: list[MemoryUnit]) -> list[MemoryUnit]:
        """Resolve every schema entity group in-place and return ``candidates``."""

        observations = _group_observations(candidates)
        existing_cache: dict[tuple[str, ...], list[_EntityView]] = {}
        for observation in observations:
            scope = observation.units[0].scope
            cache_key = (*_scope_key(scope), observation.schema_name)
            existing = existing_cache.get(cache_key)
            if existing is None:
                existing = self._load_existing_entities(
                    scope,
                    schema_name=observation.schema_name,
                )
                existing_cache[cache_key] = existing

            resolution, action = self._resolve_one(observation, existing)
            self._apply_resolution(observation, resolution, action)
            cached = _EntityView(
                entity_id=resolution.entity_id,
                entity_name=resolution.entity_name,
                entity_type=resolution.entity_type,
                description=resolution.description or observation.description,
                aliases=_dedupe_strings(
                    [*resolution.aliases, observation.name, *observation.aliases]
                ),
                identity_kind=resolution.identity_kind or observation.identity_kind,
            )
            for index, current in enumerate(existing):
                if current.entity_id == cached.entity_id:
                    existing[index] = cached
                    break
            else:
                existing.append(cached)
        return candidates

    def _resolve_one(
        self,
        observation: _EntityObservation,
        existing: list[_EntityView],
    ) -> tuple[_EntityView, str]:
        recalled = [
            candidate
            for candidate in self._recall_candidates(observation, existing)
            if _identity_compatible(observation, candidate)
        ]
        exact = _exact_candidate(observation, recalled)
        if exact is not None:
            return exact, "exact"

        target = self._llm_merge_target(observation, recalled)
        if target is not None:
            return target, "update"

        # MindMemOS performs a secondary exact-name lookup before committing a
        # CREATE.  Our reconstructed entity catalog is already in memory, so scan
        # it directly; this also protects exact identity when an embedding score
        # happened to exclude the entity from top-k recall.
        duplicate = _exact_candidate(observation, existing)
        if duplicate is not None:
            return duplicate, "exact"

        return (
            _EntityView(
                entity_id=str(uuid.uuid4()),
                entity_name=observation.name,
                entity_type=observation.entity_type,
                description=observation.description,
                aliases=list(observation.aliases),
                identity_kind=observation.identity_kind,
            ),
            "create",
        )

    def _recall_candidates(
        self,
        observation: _EntityObservation,
        existing: list[_EntityView],
    ) -> list[_EntityView]:
        indexed = self._indexed_candidates(observation)
        if indexed is not None:
            return indexed
        if not existing:
            return []
        texts = [_observation_embedding_text(observation)]
        texts.extend(_view_embedding_text(view) for view in existing)
        try:
            vectors = self._embedder.embed(texts)
            if len(vectors) != len(texts):
                raise ValueError("embedder returned a different number of entity vectors")
        except Exception as exc:
            logger.warning("SchemaEntityResolver: entity recall failed; CREATE safely: %s", exc)
            return []

        query_vector = vectors[0]
        ranked = sorted(
            (
                (view, _cosine(query_vector, vector))
                for view, vector in zip(existing, vectors[1:], strict=True)
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        return [view for view, score in ranked if score > 0.0][: self._recall_top_k]

    def _indexed_candidates(
        self,
        observation: _EntityObservation,
    ) -> list[_EntityView] | None:
        if not self._storage.has_vector_port("schema_entities"):
            return None
        try:
            vector = self._embedder.embed_query(_observation_embedding_text(observation))
            hits = self._storage.vector_port("schema_entities").search(
                observation.units[0].scope,
                VectorQuery(
                    vector=vector,
                    top_k=self._recall_top_k * 5,
                    filters=FilterClause("schema_name", FilterOp.EQ, observation.schema_name),
                    return_metadata=True,
                ),
            )
            if not hits:
                return None
            views: list[_EntityView] = []
            seen: set[str] = set()
            for hit in hits:
                metadata = hit.metadata if isinstance(hit.metadata, dict) else {}
                entity_id = str(
                    metadata.get("entity_vector_owner_id")
                    or metadata.get("schema_entity_id")
                    or hit.id.split("#sf", 1)[0]
                ).strip()
                if not entity_id or entity_id in seen:
                    continue
                seen.add(entity_id)
                try:
                    raw = self._storage.kv.get(
                        observation.units[0].scope,
                        schema_entity_key(entity_id),
                    )
                except Exception:
                    logger.warning(
                        "SchemaEntityResolver: failed to load recalled entity %s",
                        entity_id,
                        exc_info=True,
                    )
                    continue
                unit = loads(raw)
                if (
                    unit is not None
                    and unit.lifecycle is LifecycleState.ACTIVE
                    and str(unit.system_metadata.get("schema_name") or "")
                    == observation.schema_name
                ):
                    views.append(_view_from_entity_unit(unit))
                    if len(views) >= self._recall_top_k:
                        break
            return views
        except Exception as exc:
            logger.warning(
                "SchemaEntityResolver: entity index recall failed; use KV fallback: %s",
                exc,
            )
            return None

    def _llm_merge_target(
        self,
        observation: _EntityObservation,
        candidates: list[_EntityView],
    ) -> _EntityView | None:
        if not candidates or not self._enable_merge_decision:
            return None

        name_to_view: dict[str, _EntityView] = {}
        for candidate in candidates:
            name_to_view.setdefault(candidate.entity_name, candidate)
        existing_text = "\n".join(
            f"- name: {candidate.entity_name}, entity_type: {candidate.entity_type}, "
            f"Description: {candidate.description}"
            for candidate in candidates
        )
        prompt = (
            _ENTITY_MERGE_PROMPT.replace("{entity_name}", observation.name)
            .replace("{entity_type}", observation.entity_type)
            .replace("{entity_description}", observation.description[:200])
            .replace("{existing_entities}", existing_text or "No existing entities.")
        )

        for attempt in range(self._max_merge_retries):
            try:
                response = self._llm.chat(
                    [ChatMessage(role="user", content=prompt)],
                    temperature=0,
                    max_tokens=512,
                )
                decision = _parse_json_object(response)
                action = str(decision.get("action") or "").strip().lower()
                if action == "create":
                    return None
                if action == "update":
                    target_name = str(decision.get("target_entity") or "").strip()
                    target = name_to_view.get(target_name)
                    if target is None:
                        matched_name = _fuzzy_candidate_name(target_name, name_to_view)
                        target = name_to_view.get(matched_name or "")
                    if target is not None:
                        return target
                    prompt += (
                        "\nPrevious answer selected an entity outside the candidate list. "
                        f"Available names: {list(name_to_view)}. Return corrected JSON."
                    )
                    continue
                prompt += "\nPrevious answer was invalid. action must be create or update."
            except Exception as exc:
                logger.warning(
                    "SchemaEntityResolver: merge decision attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_merge_retries,
                    exc,
                )

        # MindMemOS' fail-safe: same-name candidate updates; everything else creates.
        exact_name = next(
            (candidate for candidate in candidates if candidate.entity_name == observation.name),
            None,
        )
        return exact_name

    def _load_existing_entities(
        self,
        scope: Scope,
        *,
        schema_name: str,
    ) -> list[_EntityView]:
        entity_entries = self._storage.kv.scan(scope, SCHEMA_ENTITY_KEY_PREFIX)
        matching_entity_units: list[MemoryUnit] = []
        for _key, raw in entity_entries:
            unit = loads(raw)
            if unit is None:
                continue
            if unit.lifecycle is not LifecycleState.ACTIVE:
                continue
            if str(unit.system_metadata.get("schema_name") or "") != schema_name:
                continue
            matching_entity_units.append(unit)
        entity_views = [
            _view_from_entity_unit(unit)
            for unit in matching_entity_units[: self._kv_fallback_limit]
        ]
        if entity_views:
            if len(matching_entity_units) > self._kv_fallback_limit:
                logger.warning(
                    "SchemaEntityResolver: entity registry capped at %d of %d",
                    self._kv_fallback_limit,
                    len(matching_entity_units),
                )
            return entity_views

        filters = FilterGroup(
            FilterLogic.AND,
            [
                FilterClause("system_metadata.extraction_mode", FilterOp.EQ, _SCHEMA_MODE),
                FilterClause("system_metadata.schema_name", FilterOp.EQ, schema_name),
                FilterClause("lifecycle", FilterOp.EQ, LifecycleState.ACTIVE.value),
            ],
        )
        page = self._storage.list(
            scope,
            limit=self._kv_fallback_limit,
            filters=filters,
        )
        if page.count > self._kv_fallback_limit:
            logger.warning(
                "SchemaEntityResolver: entity reconstruction capped memories at %d of %d",
                self._kv_fallback_limit,
                page.count,
            )

        grouped: dict[str, list[MemoryUnit]] = {}
        for unit in page.items:
            entity_id = str(
                unit.system_metadata.get("schema_entity_id")
                or unit.system_metadata.get("schema_entity_key")
                or ""
            ).strip()
            if entity_id:
                grouped.setdefault(entity_id, []).append(unit)
        views: list[_EntityView] = []
        for entity_id, units in grouped.items():
            entity_names = {
                _base_entity_name(str(unit.system_metadata.get("schema_entity_name") or ""))
                for unit in units
            }
            entity_names.discard("")
            if len(entity_names) > 1:
                # Older agent-memory builds mapped every entity_type=user to
                # Scope.user.  A single legacy key can therefore contain Caroline,
                # Melanie, and other unrelated people.  Reusing it would preserve
                # the corruption, so quarantine it from identity recall; new
                # observations receive clean canonical ids.
                logger.warning(
                    "SchemaEntityResolver: skipping contaminated legacy entity %s " "with names=%s",
                    entity_id,
                    sorted(entity_names),
                )
                continue
            views.append(_view_from_units(entity_id, units))
        return views

    @staticmethod
    def _apply_resolution(
        observation: _EntityObservation,
        resolved: _EntityView,
        action: str,
    ) -> None:
        observed_names = _dedupe_strings(
            [observation.name, *observation.aliases, *resolved.aliases]
        )
        aliases = [name for name in observed_names if name != resolved.entity_name]
        for unit in observation.units:
            unit.entities = [resolved.entity_name]
            unit.system_metadata["schema_entity_id"] = resolved.entity_id
            unit.system_metadata["schema_entity_key"] = resolved.entity_id
            unit.system_metadata["schema_entity_name"] = resolved.entity_name
            unit.system_metadata["schema_entity_normalized_name"] = _base_entity_name(
                resolved.entity_name
            )
            unit.system_metadata["schema_entity_aliases"] = aliases
            unit.system_metadata["schema_entity_observed_name"] = observation.name
            unit.system_metadata["schema_entity_resolution"] = action


def _group_observations(candidates: list[MemoryUnit]) -> list[_EntityObservation]:
    groups: dict[tuple[str, ...], list[MemoryUnit]] = {}
    order: list[tuple[str, ...]] = []
    for candidate in candidates:
        if candidate.system_metadata.get("extraction_mode") not in {
            _SCHEMA_MODE,
            "schema_entity_observation",
        }:
            continue
        provisional_key = str(candidate.system_metadata.get("schema_entity_key") or "").strip()
        key = (
            *_scope_key(candidate.scope),
            str(candidate.system_metadata.get("schema_name") or ""),
            str(candidate.system_metadata.get("schema_entity_type") or ""),
            provisional_key
            or str(candidate.system_metadata.get("schema_entity_normalized_name") or ""),
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)

    observations: list[_EntityObservation] = []
    for key in order:
        units = groups[key]
        representative = units[0]
        name = str(representative.system_metadata.get("schema_entity_name") or "").strip()
        entity_type = str(representative.system_metadata.get("schema_entity_type") or "").strip()
        schema_name = str(representative.system_metadata.get("schema_name") or "").strip()
        aliases = _entity_aliases(units)
        descriptions = _entity_descriptions(units)
        observations.append(
            _EntityObservation(
                units=units,
                name=name,
                entity_type=entity_type,
                schema_name=schema_name,
                aliases=aliases,
                description=" ".join(descriptions[:5]),
                identity_kind=str(
                    representative.system_metadata.get("schema_entity_identity_kind") or ""
                ),
            )
        )
    return observations


def _view_from_units(entity_id: str, units: list[MemoryUnit]) -> _EntityView:
    representative = units[0]
    name = str(representative.system_metadata.get("schema_entity_name") or "").strip()
    entity_type = str(representative.system_metadata.get("schema_entity_type") or "").strip()
    aliases = _entity_aliases(units)
    descriptions = _entity_descriptions(units)
    return _EntityView(
        entity_id=entity_id,
        entity_name=name,
        entity_type=entity_type,
        description=" ".join(descriptions[:5]),
        aliases=aliases,
        identity_kind=str(representative.system_metadata.get("schema_entity_identity_kind") or ""),
    )


def _view_from_entity_unit(unit: MemoryUnit) -> _EntityView:
    return _EntityView(
        entity_id=str(unit.system_metadata.get("schema_entity_id") or unit.id),
        entity_name=str(unit.system_metadata.get("schema_entity_name") or ""),
        entity_type=str(unit.system_metadata.get("schema_entity_type") or ""),
        description=unit.content,
        aliases=_string_list(unit.system_metadata.get("schema_entity_aliases")),
        identity_kind=str(unit.system_metadata.get("schema_entity_identity_kind") or ""),
    )


def _exact_candidate(
    observation: _EntityObservation,
    candidates: list[_EntityView],
) -> _EntityView | None:
    target_name = _base_entity_name(observation.name)
    for candidate in candidates:
        if (
            _base_entity_name(candidate.entity_name) == target_name
            and candidate.entity_type == observation.entity_type
        ):
            return candidate
    return None


def _identity_compatible(
    observation: _EntityObservation,
    candidate: _EntityView,
) -> bool:
    observation_generic = _is_generic_name(observation.name)
    candidate_generic = _is_generic_name(candidate.entity_name)
    if observation_generic != candidate_generic:
        return False
    if observation.identity_kind == "explicit_speaker":
        observed = _base_entity_name(observation.name)
        known_names = {
            _base_entity_name(name) for name in [candidate.entity_name, *candidate.aliases] if name
        }
        if observed not in known_names:
            return False
    return True


def _is_generic_name(value: str) -> bool:
    return _base_entity_name(value) in {
        "user",
        "assistant",
        "speaker",
        "participant",
        "person",
        "interlocutor",
        "用户",
        "助手",
        "说话者",
    }


def _base_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _NAME_SUFFIX_RE.sub("", normalized).strip()
    return " ".join(normalized.split()).casefold()


def _fuzzy_candidate_name(
    target_name: str,
    name_to_view: dict[str, _EntityView],
) -> str | None:
    target = target_name.casefold()
    for name in name_to_view:
        if name.casefold() == target:
            return name
    for name in name_to_view:
        candidate = name.casefold()
        if target in candidate or candidate in target:
            return name
    return None


def _observation_embedding_text(observation: _EntityObservation) -> str:
    return _embedding_text(
        name=observation.name,
        entity_type=observation.entity_type,
        description=observation.description,
        aliases=observation.aliases,
    )


def _view_embedding_text(view: _EntityView) -> str:
    return _embedding_text(
        name=view.entity_name,
        entity_type=view.entity_type,
        description=view.description,
        aliases=view.aliases,
    )


def _entity_aliases(units: list[MemoryUnit]) -> list[str]:
    aliases: list[str] = []
    for unit in units:
        aliases.extend(_string_list(unit.system_metadata.get("schema_entity_aliases")))
    return _dedupe_strings(aliases)


def _entity_descriptions(units: list[MemoryUnit]) -> list[str]:
    descriptions: list[str] = []
    for unit in units:
        description = str(
            unit.system_metadata.get("schema_entity_description") or ""
        ).strip()
        if description:
            descriptions.append(description)
    if descriptions:
        return _dedupe_strings(descriptions)

    for unit in units:
        for segment in unit.segments:
            content = segment.content.strip()
            if content:
                descriptions.append(content)
    return _dedupe_strings(descriptions)


def _embedding_text(
    *,
    name: str,
    entity_type: str,
    description: str,
    aliases: list[str],
) -> str:
    non_empty_parts: list[str] = []
    for part in (name, entity_type, description, " ".join(aliases[:5])):
        if part:
            non_empty_parts.append(part)
    return " ".join(non_empty_parts)


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _parse_json_object(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("entity merge response is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dedupe_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
