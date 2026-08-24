"""Entity Schema constrained property extraction.

Every accepted ``entities[].properties[]`` item becomes one standard ``MemoryUnit``.
The extractor does not persist data, resolve canonical identities, merge properties,
or project graph relations.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jiuwen_memory.common._support import as_bool
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    ChatMessage,
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    Segment,
    Temporal,
    inherited_system_metadata,
    inherited_user_metadata,
)
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.common import merge_unit_tags
from jiuwen_memory.construction.entity_schema import EntitySchemaCatalog
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    ExtractorImpl,
    InvalidExtractionJSONError,
    _format_context_block,
)
from jiuwen_memory.construction.schema_prompts import (
    ENTITY_GENERATION_PROMPT,
    SCHEMA_SELECTION_FOR_GENERATION_PROMPT,
)

logger = get_logger(__name__)

_DEFAULT_BATCH_SIZE = 8
_DEFAULT_VALIDATION_ATTEMPTS = 3
_DEFAULT_MAX_ENTITIES = 200
_DEFAULT_MAX_PROPERTIES = 15
_NAME_SUFFIX_RE = re.compile(r"[（(].*$")
_PROPERTY_TIME_TOKEN_RE = re.compile(
    r"(?<![\d-])"
    r"(\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
    r"|\d{4}-\d{2}|\d{4})"
    r"(?![\d-])"
)
_SPEAKER_LABEL_RE = re.compile(
    r"^\s*(?:speaker=(?P<speaker>[^\r\n:]{1,100})|\[(?P<bracket>[^\]\r\n]{1,100})\])\s*:",
    flags=re.IGNORECASE,
)
_GENERIC_ENTITY_NAMES = {
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
_SCHEMA_INTERNAL_METADATA_KEYS = frozenset(
    {
        "entity_search_field",
        "entity_search_field_index",
        "entity_vector_owner_id",
        "entity_vector_role",
        "extracted_statement",
        "extraction_mode",
        "memory_role",
        "record_kind",
        "schema_source_evidence",
        "target",
    }
)

_SCHEMA_SELECTION_SYSTEM_PROMPT = SCHEMA_SELECTION_FOR_GENERATION_PROMPT
_ENTITY_GENERATION_SYSTEM_PROMPT = ENTITY_GENERATION_PROMPT


class InvalidSchemaExtractionError(ValueError):
    """Schema 抽取响应在重试后仍不满足结构契约。"""


@dataclass
class SchemaPropertyCandidate:
    """Extractor 内部的一条实体属性候选。"""

    entity_name: str
    entity_type: str
    property_name: str
    value: str
    property_time: str
    source_unit_ids: list[str]


class SchemaExtractionNormalizer:
    """Normalize one LLM answer against the exact schema selected for this round."""

    def __init__(
        self,
        catalog: EntitySchemaCatalog,
        *,
        max_entities_per_conversation: int = _DEFAULT_MAX_ENTITIES,
        max_properties_per_entity: int = _DEFAULT_MAX_PROPERTIES,
    ) -> None:
        self._catalog = catalog
        self._max_entities = max_entities_per_conversation
        self._max_properties = max_properties_per_entity

    def normalize_and_validate(
        self,
        raw_memory: dict[str, Any],
        dialogue_timestamp: str,
        *,
        entity_schema: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        del dialogue_timestamp
        raw = copy.deepcopy(raw_memory)
        errors: list[str] = []

        entities = raw.setdefault("entities", [])
        if not isinstance(entities, list):
            return raw, ["entities must be an array"]
        names = [
            str(entity.get("name")).strip()
            for entity in entities
            if isinstance(entity, dict) and entity.get("name")
        ]
        if len(names) != len(set(names)):
            errors.append("entity names must be unique within one response")

        if len(entities) > self._max_entities:
            logger.warning(
                "EntitySchemaExtractor: truncating entities from %d to %d",
                len(entities),
                self._max_entities,
            )
            entities = entities[: self._max_entities]

        validation_schema = (
            entity_schema if entity_schema is not None else self._catalog.get_all_dicts()
        )
        selected_properties = _schema_property_names(validation_schema)
        valid_types = set(selected_properties)
        catalog_types = set(self._catalog.list_types())
        prepared_entities: list[dict[str, Any]] = []

        for entity_index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                errors.append(f"entity {entity_index} must be an object")
                continue
            name = str(entity.get("name") or "").strip()
            if not name:
                errors.append(f"entity {entity_index} has empty name")
                continue
            entity["name"] = name

            entity_type = str(entity.get("entity_type") or "").strip()
            if entity_type not in valid_types:
                if entity_type in catalog_types:
                    errors.append(
                        f"entity {name!r} type {entity_type!r} was not selected for this extraction"
                    )
                else:
                    errors.append(f"entity {name!r} has unsupported type {entity_type!r}")
                continue

            properties = entity.setdefault("properties", [])
            if not isinstance(properties, list):
                errors.append(f"entity {name!r} properties must be an array")
                continue

            valid_properties = selected_properties.get(entity_type, set())
            catalog_schema = self._catalog.get(entity_type)
            catalog_properties = catalog_schema.all_property_names() if catalog_schema else set()
            has_default = "default_property" in valid_properties
            prepared_properties: list[dict[str, Any]] = []
            for property_index, prop in enumerate(properties):
                if not isinstance(prop, dict):
                    errors.append(f"entity {name!r} property {property_index} must be an object")
                    continue
                property_name = str(prop.get("property_name") or "").strip()
                if property_name not in valid_properties:
                    if property_name in catalog_properties:
                        errors.append(
                            f"entity {name!r} property {property_name!r} was not selected "
                            "for this extraction"
                        )
                        continue
                    if not has_default:
                        errors.append(f"entity {name!r} has unsupported property {property_name!r}")
                        continue
                    logger.warning(
                        "EntitySchemaExtractor: repair entity %r property %r to default_property",
                        name,
                        property_name,
                    )
                    property_name = "default_property"
                    prop["property_name"] = property_name
                value = prop.get("value")
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"entity {name!r} property {property_name!r} has empty/non-string value"
                    )
                    continue
                prop["value"] = value.strip()
                # Event time is optional enrichment in this PR. A malformed or inconsistent
                # timestamp must not discard an otherwise valid fact.
                prop["time"] = _normalize_property_time(prop.get("time"), prop["value"])
                prepared_properties.append(prop)
            if len(prepared_properties) > self._max_properties:
                logger.warning(
                    "EntitySchemaExtractor: truncating entity %r properties from %d to %d",
                    name,
                    len(prepared_properties),
                    self._max_properties,
                )
            entity["properties"] = prepared_properties[: self._max_properties]
            prepared_entities.append(entity)

        raw["entities"] = prepared_entities
        return raw, errors


class EntitySchemaExtractor(Extractor):
    """由 Entity Schema 约束、每个 property 产一个 MemoryUnit 的 Extractor。"""

    def __init__(
        self,
        llm: LLM,
        schema: EntitySchemaCatalog | str,
        *,
        enable_schema_selection: bool = False,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        validation_attempts: int = _DEFAULT_VALIDATION_ATTEMPTS,
        max_entities_per_conversation: int = _DEFAULT_MAX_ENTITIES,
        max_properties_per_entity: int = _DEFAULT_MAX_PROPERTIES,
        retry_max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        if batch_size < 1:
            raise ValueError("schema batch_size 必须 >= 1")
        if validation_attempts < 1:
            raise ValueError("schema validation_attempts 必须 >= 1")
        if max_entities_per_conversation < 1:
            raise ValueError("max_entities_per_conversation 必须 >= 1")
        if max_properties_per_entity < 1:
            raise ValueError("max_properties_per_entity 必须 >= 1")
        self._llm = llm
        self._catalog = (
            schema
            if isinstance(schema, EntitySchemaCatalog)
            else EntitySchemaCatalog.from_file(schema)
        )
        self._enable_schema_selection = enable_schema_selection
        self._batch_size = batch_size
        self._validation_attempts = validation_attempts
        self._normalizer = SchemaExtractionNormalizer(
            self._catalog,
            max_entities_per_conversation=max_entities_per_conversation,
            max_properties_per_entity=max_properties_per_entity,
        )
        self._helper = ExtractorImpl(
            llm=llm,
            retry_max_retries=retry_max_retries,
            retry_backoff_ms=retry_backoff_ms,
        )

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        self._helper.health()

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        accepted = self._helper.preprocess(units)
        if not accepted:
            return []

        result: list[MemoryUnit] = []
        errors: list[Exception] = []
        successful_batches = 0
        for start in range(0, len(accepted), self._batch_size):
            batch = accepted[start:start + self._batch_size]
            try:
                result.extend(self._extract_batch(batch, context=context))
                successful_batches += 1
            except Exception as exc:
                errors.append(exc)
                logger.warning(
                    "EntitySchemaExtractor: batch start=%d size=%d failed: %s",
                    start,
                    len(batch),
                    exc,
                )

        if successful_batches == 0 and errors:
            raise errors[-1]
        return _dedupe_units(result)

    def _extract_batch(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None,
    ) -> list[MemoryUnit]:
        full_schema = self._catalog.schema_for_generation()
        if not full_schema:
            return []
        selected_schema = self._select_schema(units, full_schema)
        if not selected_schema:
            return []
        dialogue_timestamp = _dialogue_timestamp(units)
        candidates = self._extract_candidates(
            units,
            selected_schema,
            dialogue_timestamp=dialogue_timestamp,
            context=context,
        )
        return self._build_units(candidates, units)

    def _select_schema(
        self,
        units: list[MemoryUnit],
        full_schema: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self._enable_schema_selection:
            return full_schema
        try:
            summary = _format_schema_summary(full_schema)
            conversation = "\n".join(unit.content for unit in units)[:2000]
            prompt = _SCHEMA_SELECTION_SYSTEM_PROMPT.format(
                dialogue_text=conversation,
                entity_schema=summary,
            )
            response = self._helper.call_llm_with_retry(
                [ChatMessage(role="user", content=prompt)],
                max_tokens=2048,
            )
            payload = _parse_json_object(response)
            selected = payload.get("selected_entities")
            if not isinstance(selected, list):
                raise ValueError("schema selection selected_entities must be an array")
            return self._catalog.filter_selected(full_schema, selected)
        except Exception:
            logger.warning(
                "EntitySchemaExtractor: schema selection failed; using full schema",
                exc_info=True,
            )
            return full_schema

    def _extract_candidates(
        self,
        units: list[MemoryUnit],
        entity_schema: list[dict[str, Any]],
        *,
        dialogue_timestamp: str,
        context: ExtractContext | None,
    ) -> list[SchemaPropertyCandidate]:
        source_text = "\n".join(
            f"[message_index={index}, unit_id={unit.id}]\n{unit.content}"
            for index, unit in enumerate(units)
        )
        context_block = _format_context_block(context)
        prompt = (
            _ENTITY_GENERATION_SYSTEM_PROMPT.replace(
                "{entity_schema}", json.dumps(entity_schema, ensure_ascii=False)
            )
            .replace("{dialogue_timestamp}", dialogue_timestamp)
            .replace("{chat_chunk}", source_text)
        )
        if context_block:
            prompt += f"\n\n{context_block}"

        last_errors: list[str] = []
        last_response = ""
        best_candidates: list[SchemaPropertyCandidate] = []
        for _attempt in range(self._validation_attempts):
            response = self._helper.call_llm_with_retry([ChatMessage(role="user", content=prompt)])
            last_response = response
            try:
                raw_memory = _parse_json_object(response)
            except InvalidExtractionJSONError as exc:
                last_errors = [str(exc)]
            else:
                normalized, validation_errors = self._normalizer.normalize_and_validate(
                    raw_memory,
                    dialogue_timestamp,
                    entity_schema=entity_schema,
                )
                candidates, candidate_errors = self._build_candidates(normalized, units)
                last_errors = [*validation_errors, *candidate_errors]
                if not last_errors:
                    return candidates
                if candidates and len(candidates) >= len(best_candidates):
                    best_candidates = candidates
            prompt += (
                "\n\nPrevious answer:\n"
                + last_response
                + "\nERRORS:\n- "
                + "\n- ".join(last_errors)
                + "\nReturn a corrected complete JSON object."
            )

        # Keep valid properties from the best response even when sibling items are bad.
        # This follows Agent Memory's construction integrity rule: isolate bad
        # candidates, and fail a batch only when it produces no usable candidate.
        if best_candidates:
            logger.warning(
                "EntitySchemaExtractor: validation retries exhausted; preserving valid "
                "properties and dropping invalid siblings: %s",
                "; ".join(last_errors),
            )
            return best_candidates

        details = "; ".join(last_errors) or "unknown schema extraction error"
        raise InvalidSchemaExtractionError(
            f"schema extraction failed after {self._validation_attempts} attempts: {details}"
        )

    def _build_candidates(
        self,
        raw_memory: dict[str, Any],
        units: list[MemoryUnit],
    ) -> tuple[list[SchemaPropertyCandidate], list[str]]:
        unit_map = {unit.id: unit for unit in units}
        candidates: list[SchemaPropertyCandidate] = []
        invalid_reasons: list[str] = []

        for entity in raw_memory.get("entities", []):
            if not isinstance(entity, dict):
                continue
            entity_name = str(entity.get("name") or "").strip()
            entity_type = str(entity.get("entity_type") or "").strip()
            for prop in entity.get("properties", []):
                if not isinstance(prop, dict):
                    continue
                property_name = str(prop.get("property_name") or "").strip()
                explicit_sources = prop.get("source_unit_ids")
                if isinstance(explicit_sources, list):
                    source_ids = _dedupe_strings(explicit_sources)
                else:
                    source_ids = []

                unknown_sources = [
                    source_id for source_id in source_ids if source_id not in unit_map
                ]
                if not source_ids or unknown_sources:
                    invalid_reasons.append(
                        f"{entity_name}.{property_name} has invalid source_unit_ids "
                        f"{source_ids!r}; unknown={unknown_sources!r}"
                    )
                    continue
                source_scopes = {_scope_identity(unit_map[source_id]) for source_id in source_ids}
                if len(source_scopes) != 1:
                    invalid_reasons.append(
                        f"{entity_name}.{property_name} sources cross MemoryUnit scopes"
                    )
                    continue

                source_speakers = _source_speaker_names(
                    [unit_map[source_id] for source_id in source_ids]
                )
                property_value = str(prop.get("value") or "").strip()
                resolved_name = entity_name
                named_source_speakers = [
                    name
                    for name in source_speakers
                    if _normalize_entity_name(name) == _normalize_entity_name(entity_name)
                ]
                speaker_name = (
                    named_source_speakers[0]
                    if len(named_source_speakers) == 1
                    else _speaker_for_property(property_value, source_speakers)
                )
                if source_speakers and _is_generic_entity_name(entity_name) and not speaker_name:
                    invalid_reasons.append(
                        f"{entity_name}.{property_name} cannot bind generic identity to one "
                        f"explicit source speaker from {source_speakers!r}"
                    )
                    continue
                if speaker_name:
                    if _is_generic_entity_name(entity_name):
                        resolved_name = speaker_name

                candidates.append(
                    SchemaPropertyCandidate(
                        entity_name=resolved_name,
                        entity_type=entity_type,
                        property_name=property_name,
                        value=property_value,
                        property_time=str(prop.get("time") or ""),
                        source_unit_ids=source_ids,
                    )
                )

        return candidates, invalid_reasons

    def _build_units(
        self,
        candidates: list[SchemaPropertyCandidate],
        sources: list[MemoryUnit],
    ) -> list[MemoryUnit]:
        source_map = {source.id: source for source in sources}
        result: list[MemoryUnit] = []
        for candidate in candidates:
            source_units = [source_map[source_id] for source_id in candidate.source_unit_ids]
            primary = source_units[0]
            now = datetime.now(timezone.utc)
            source_tags = [tag for source in source_units for tag in source.tags]
            metadata = _inherited_schema_system_metadata(source_units)
            metadata.update(
                {
                    "extraction_mode": "schema",
                    "schema_name": self._catalog.schema_name,
                    "schema_version": self._catalog.schema_version,
                    "schema_entity_name": candidate.entity_name,
                    "schema_entity_type": candidate.entity_type,
                    "schema_property_name": candidate.property_name,
                }
            )
            event_fields = _event_time_fields(candidate.property_time)
            result.append(
                MemoryUnit(
                    id=str(uuid.uuid4()),
                    scope=primary.scope,
                    tier=_schema_memory_tier(candidate.entity_type, candidate.property_name),
                    segments=[Segment(content=candidate.value, source=primary.source)],
                    source_ref=primary.id,
                    temporal=Temporal(
                        **event_fields,
                        t_ingest=now,
                        t_valid=now,
                        t_message=primary.temporal.t_message,
                    ),
                    provenance=list(candidate.source_unit_ids),
                    tags=merge_unit_tags(source_tags, ["extracted", "schema"]),
                    system_metadata=metadata,
                    user_metadata=inherited_user_metadata(source_units),
                    lifecycle=LifecycleState.ACTIVE,
                    entities=[candidate.entity_name],
                )
            )
        return result


def _parse_json_object(response: str) -> dict[str, Any]:
    """Parse one exact JSON object, allowing only a complete Markdown fence wrapper.

    Schema validation must inspect the response root.  Scanning forward to an inner ``{`` can
    accidentally accept an example object embedded in prose and silently discard the real answer.
    """

    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidExtractionJSONError("schema extractor response is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidExtractionJSONError(
            f"schema extractor response must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _format_schema_summary(schema: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entity in schema:
        entity_type = str(entity.get("entity_type") or "")
        lines.append(f"## {entity_type}: {entity.get('entity_description', '')}")
        for field_name in ("static_property", "dynamic_property"):
            properties = entity.get(field_name, {})
            if not isinstance(properties, dict):
                continue
            for name, definition in properties.items():
                if name == "default_property":
                    continue
                description = (
                    str(definition.get("desc") or definition.get("description") or "")
                    if isinstance(definition, dict)
                    else str(definition)
                )
                lines.append(f"- {name}: {description[:80]}")
    return "\n".join(lines)


def _schema_property_names(
    entity_schema: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Build the exact entity/property allowlist used for this generation round."""
    result: dict[str, set[str]] = {}
    for item in entity_schema:
        if not isinstance(item, dict):
            continue
        entity_type = str(item.get("entity_type") or "").strip()
        if not entity_type:
            continue
        property_names: set[str] = set()
        for field_name in ("static_property", "dynamic_property"):
            properties = item.get(field_name, {})
            if isinstance(properties, dict):
                property_names.update(str(name) for name in properties)
        result[entity_type] = property_names
    return result


def _dialogue_timestamp(units: list[MemoryUnit]) -> str:
    for unit in units:
        observation_date = str(unit.system_metadata.get("observation_date") or "").strip()
        if observation_date:
            return observation_date
    for unit in units:
        if unit.temporal.t_message is not None:
            return unit.temporal.t_message.isoformat()
    # Compatibility for raw-message units written before Temporal.t_message existed.  Those units
    # stored their message timestamp in t_event.
    for unit in units:
        if unit.temporal.t_event is not None:
            return unit.temporal.t_event.isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def _scope_identity(unit: MemoryUnit) -> tuple[str, str, str, str, str]:
    scope = unit.scope
    return scope.org, scope.space, scope.user, scope.agent, scope.session


def _event_time_fields(value: str) -> dict[str, Any]:
    """Project complete day/datetime values into mem2.0's native ``Temporal.t_event``."""

    text = str(value or "").strip()
    event = _parse_complete_event_time(text)
    return {"t_event": event} if event is not None else {}


def _parse_complete_event_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text or re.fullmatch(r"\d{4}(?:-\d{2})?", text):
        return None
    normalized = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_property_time(raw_time: Any, value: str) -> str:
    """Return optional event-time enrichment without rejecting the property fact."""

    value_times = _property_time_tokens(value)
    time_text = raw_time.strip() if isinstance(raw_time, str) else ""
    if time_text and _is_valid_property_time(time_text):
        if not value_times or any(
            _property_times_compatible(time_text, item) for item in value_times
        ):
            return time_text
    return value_times[0] if len(value_times) == 1 else ""


def _property_time_tokens(value: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in _PROPERTY_TIME_TOKEN_RE.finditer(value)))


def _is_valid_property_time(value: str) -> bool:
    if re.fullmatch(r"\d{4}", value):
        return True
    if re.fullmatch(r"\d{4}-\d{2}", value):
        try:
            datetime.strptime(value, "%Y-%m")
        except ValueError:
            return False
        return True
    return _parse_complete_event_time(value) is not None


def _property_times_compatible(first: str, second: str) -> bool:
    first_precision = _property_time_precision(first)
    if first_precision != _property_time_precision(second):
        return False
    if first_precision == "datetime":
        return _parse_complete_event_time(first) == _parse_complete_event_time(second)
    return first == second


def _property_time_precision(value: str) -> str:
    if len(value) == 4:
        return "year"
    if len(value) == 7:
        return "month"
    if len(value) == 10:
        return "date"
    return "datetime"


def _normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _NAME_SUFFIX_RE.sub("", normalized).strip()
    return " ".join(normalized.split()).casefold()


def _is_generic_entity_name(value: str) -> bool:
    return _normalize_entity_name(value) in _GENERIC_ENTITY_NAMES


def _source_speaker_names(units: list[MemoryUnit]) -> list[str]:
    names: list[str] = []
    for unit in units:
        match = _SPEAKER_LABEL_RE.match(unit.content)
        if match:
            name = " ".join((match.group("speaker") or match.group("bracket")).strip().split())
            if name and _normalize_entity_name(name) not in {
                _normalize_entity_name(item) for item in names
            }:
                names.append(name)
    return names


def _speaker_for_property(value: str, speakers: list[str]) -> str:
    if len(speakers) == 1:
        return speakers[0]
    mentioned = [
        name
        for name in speakers
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", value, flags=re.IGNORECASE)
    ]
    return mentioned[0] if len(mentioned) == 1 else ""


def _inherited_schema_system_metadata(
    sources: list[MemoryUnit],
) -> dict[str, Any]:
    """Inherit source context without carrying an earlier Schema derivation state."""

    inherited = inherited_system_metadata(sources)
    result: dict[str, Any] = {}
    for key, value in inherited.items():
        if key in _SCHEMA_INTERNAL_METADATA_KEYS:
            continue
        if key.startswith("schema_") or key.startswith("property_merge_"):
            continue
        result[key] = value
    return result


def _schema_memory_tier(entity_type: str, property_name: str) -> MemoryTier:
    normalized_type = entity_type.strip().lower().replace("-", "_").replace(" ", "_")
    normalized_property = property_name.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized_type in {"episode", "episodes", "episodic"}:
        raise ValueError("episode schema memories are not supported")
    if "task_experience" in {normalized_type, normalized_property}:
        return MemoryTier.PROCEDURAL
    if normalized_type in {"person", "user"}:
        return MemoryTier.CORE
    return MemoryTier.SEMANTIC


def _dedupe_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dedupe_units(units: list[MemoryUnit]) -> list[MemoryUnit]:
    result: list[MemoryUnit] = []
    seen: dict[tuple[str, ...], MemoryUnit] = {}
    for unit in units:
        event_time = unit.temporal.t_event
        key = (
            *_scope_identity(unit),
            str(unit.system_metadata.get("schema_name") or ""),
            str(unit.system_metadata.get("schema_entity_type") or ""),
            str(unit.system_metadata.get("schema_entity_name") or ""),
            str(unit.system_metadata.get("schema_property_name") or ""),
            " ".join(unit.content.split()).casefold(),
            event_time.isoformat() if event_time is not None else "",
        )
        if key in seen:
            existing = seen[key]
            existing.provenance = _dedupe_strings([*existing.provenance, *unit.provenance])
            existing.tags = merge_unit_tags(existing.tags, unit.tags)
            continue
        seen[key] = unit
        result.append(unit)
    return result


@ExtractorProducer.register("entity_schema")
def _build(config):
    schema_path = str(config.get("schema_path") or "").strip()
    if not schema_path:
        raise ValueError("entity_schema extractor requires schema_path")
    return EntitySchemaExtractor(
        llm=LlmProducer.dep(config, default="echo"),
        schema=schema_path,
        enable_schema_selection=as_bool(
            config.get("enable_schema_selection"),
            default=False,
        ),
        batch_size=int(config.get("schema_batch_size", _DEFAULT_BATCH_SIZE)),
        validation_attempts=int(
            config.get("schema_validation_attempts", _DEFAULT_VALIDATION_ATTEMPTS)
        ),
        max_entities_per_conversation=int(
            config.get("max_entities_per_conversation", _DEFAULT_MAX_ENTITIES)
        ),
        max_properties_per_entity=int(
            config.get("max_properties_per_entity", _DEFAULT_MAX_PROPERTIES)
        ),
        retry_max_retries=int(config.get("extractor_retry_max", 3)),
        retry_backoff_ms=int(config.get("extractor_retry_backoff", 1000)),
    )
