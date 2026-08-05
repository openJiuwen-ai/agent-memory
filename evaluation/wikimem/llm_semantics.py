"""LLM-backed semantic compilation primitives for the wikimem Wiki.

The module is deliberately independent from any dataset adapter.  It accepts
source records and returns a small canonical JSON-compatible ontology.  A
caller may pass ``llm=None``; in that case no network call is made and the
caller can use its deterministic fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from common.llm.base import LLM
from common.type_def.chat import ChatMessage


MEMORY_KINDS = (
    "entity",
    "fact",
    "event",
    "preference",
    "skill",
    "relationship",
    "decision",
    "constraint",
    "context",
    "artifact",
)
MemoryKind = str


@dataclass(frozen=True)
class SemanticSource:
    """A dataset-independent source block supplied to the memory compiler."""

    source_id: str
    text: str
    conversation_id: str = ""
    session_id: str = ""
    speaker: str = ""
    timestamp: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticEntity:
    name: str
    entity_type: str = "thing"
    description: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticRelation:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True)
class SemanticMemory:
    memory_id: str
    kind: MemoryKind
    content: str
    source_id: str
    evidence: str = ""
    entities: tuple[SemanticEntity, ...] = ()
    relations: tuple[SemanticRelation, ...] = ()
    timestamp: str = ""
    confidence: float = 0.0
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryUnderstanding:
    intent: str = "recall"
    entities: tuple[str, ...] = ()
    relation: str = ""
    time_expression: str = ""
    expanded_terms: tuple[str, ...] = ()
    memory_kinds: tuple[str, ...] = ()


_EXTRACTION_SYSTEM_PROMPT = """You are a long-term memory compiler.
Read source blocks and return ONLY a JSON array. Extract only information
explicitly stated in each source; do not use answers, evaluation labels, or
outside knowledge. Keep every item traceable to exactly one source_id.

Each item must have:
{"source_id":"...","kind":"entity|fact|event|preference|skill|relationship|
decision|constraint|context|artifact",
"content":"one self-contained memory statement in the source language",
"evidence":"the smallest verbatim source span that supports it",
"entities":[{"name":"...","entity_type":"...","description":"...","aliases":["..."]}],
"relations":[{"subject":"...","predicate":"...","object":"..."}],
"timestamp":"ISO-8601 or empty","confidence":0.0,"tags":["..."],"metadata":{}}

Rules: preserve negation, quantities, names, dates, and modality; do not merge
unrelated facts; emit no item for greetings or unsupported speculation; use a
confidence between 0 and 1; keep content and evidence in the source language.
"""

_QUERY_SYSTEM_PROMPT = """You are a query understanding module for a long-term memory system.
Return ONLY one JSON object with keys:
{"intent":"recall|compare|decision|preference|procedure|event|profile",
"entities":["..."],"relation":"...","time_expression":"...",
"expanded_terms":["..."],"memory_kinds":["entity|fact|event|preference|skill|relationship|
decision|constraint|context|artifact"]}
Expand paraphrases conservatively. Do not invent entities or facts.
"""


def extract_semantic_memories(
    llm: LLM,
    sources: Iterable[SemanticSource],
    *,
    batch_size: int = 8,
    max_tokens: int = 4096,
) -> list[SemanticMemory]:
    """Extract ontology records with a bounded number of LLM calls."""

    materialized = [source for source in sources if source.text.strip()]
    result: list[SemanticMemory] = []
    for start in range(0, len(materialized), max(1, batch_size)):
        batch = materialized[start : start + max(1, batch_size)]
        source_text = "\n".join(
            "---\n"
            f"source_id: {source.source_id}\n"
            f"conversation_id: {source.conversation_id}\n"
            f"session_id: {source.session_id}\n"
            f"speaker: {source.speaker}\n"
            f"timestamp: {source.timestamp}\n"
            f"metadata: {json.dumps(source.metadata, ensure_ascii=False)}\n"
            f"text: {source.text}\n---"
            for source in batch
        )
        response = llm.chat(
            [
                ChatMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                ChatMessage(role="user", content=source_text),
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        payload = _parse_json_payload(response)
        if not isinstance(payload, list):
            raise ValueError("LLM memory extraction must return a JSON array")
        valid_ids = {source.source_id for source in batch}
        for item in payload:
            memory = _parse_memory_item(item, valid_ids)
            if memory is not None:
                result.append(memory)
    return result


def understand_query(
    llm: LLM,
    question: str,
    *,
    known_entities: Iterable[str] = (),
    max_tokens: int = 768,
) -> QueryUnderstanding:
    """Add semantic intent and conservative query expansions."""

    known = ", ".join(str(value).strip() for value in known_entities if str(value).strip())
    response = llm.chat(
        [
            ChatMessage(role="system", content=_QUERY_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=f"Known entities: {known or '(none)'}\nQuestion: {question}",
            ),
        ],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    payload = _parse_json_payload(response)
    if not isinstance(payload, dict):
        raise ValueError("LLM query understanding must return a JSON object")
    return QueryUnderstanding(
        intent=_clean_scalar(payload.get("intent"), "recall"),
        entities=_clean_string_tuple(payload.get("entities")),
        relation=_clean_scalar(payload.get("relation")),
        time_expression=_clean_scalar(payload.get("time_expression")),
        expanded_terms=_clean_string_tuple(payload.get("expanded_terms")),
        memory_kinds=tuple(
            value
            for value in _clean_string_tuple(payload.get("memory_kinds"))
            if value in MEMORY_KINDS
        ),
    )


def _parse_memory_item(item: Any, valid_ids: set[str]) -> SemanticMemory | None:
    if not isinstance(item, dict):
        return None
    source_id = _clean_scalar(item.get("source_id"))
    content = _clean_scalar(item.get("content"))
    if not source_id or source_id not in valid_ids or not content:
        return None
    kind = _clean_scalar(item.get("kind"), "context").lower()
    if kind not in MEMORY_KINDS:
        kind = "context"
    confidence = _safe_confidence(item.get("confidence"))
    entities = tuple(_parse_entity(value) for value in _as_list(item.get("entities")))
    entities = tuple(value for value in entities if value is not None)
    relations = tuple(_parse_relation(value) for value in _as_list(item.get("relations")))
    relations = tuple(value for value in relations if value is not None)
    stable_key = f"{source_id}:{kind}:{content}".encode("utf-8")
    memory_id = _clean_scalar(item.get("memory_id")) or (
        f"{source_id}:{kind}:{hashlib.sha1(stable_key).hexdigest()[:16]}"
    )
    return SemanticMemory(
        memory_id=memory_id,
        kind=kind,
        content=content,
        source_id=source_id,
        evidence=_clean_scalar(item.get("evidence")),
        entities=entities,
        relations=relations,
        timestamp=_clean_scalar(item.get("timestamp")),
        confidence=confidence,
        tags=_clean_string_tuple(item.get("tags"))[:8],
        metadata={
            str(key): _clean_scalar(value)
            for key, value in item.get("metadata", {}).items()
        }
        if isinstance(item.get("metadata"), dict)
        else {},
    )


def _parse_entity(value: Any) -> SemanticEntity | None:
    if isinstance(value, str):
        name = value.strip()
        return SemanticEntity(name=name) if name else None
    if not isinstance(value, dict):
        return None
    name = _clean_scalar(value.get("name"))
    if not name:
        return None
    return SemanticEntity(
        name=name,
        entity_type=_clean_scalar(value.get("entity_type"), "thing"),
        description=_clean_scalar(value.get("description")),
        aliases=_clean_string_tuple(value.get("aliases"))[:8],
    )


def _parse_relation(value: Any) -> SemanticRelation | None:
    if not isinstance(value, dict):
        return None
    subject = _clean_scalar(value.get("subject"))
    predicate = _clean_scalar(value.get("predicate"))
    obj = _clean_scalar(value.get("object"))
    if not subject or not predicate or not obj:
        return None
    return SemanticRelation(subject=subject, predicate=predicate, object=obj)


def _parse_json_payload(response: str) -> Any:
    text = str(response or "").strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
        if not starts:
            raise
        start = min(starts)
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
        raise


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _clean_scalar(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _clean_string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(
        item
        for item in (_clean_scalar(raw) for raw in _as_list(value))
        if item
    )


def _safe_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5
