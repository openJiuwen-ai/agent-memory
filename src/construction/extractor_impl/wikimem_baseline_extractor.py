"""wikimem baseline candidate extraction for mem2.0 construction."""

from __future__ import annotations

import re
import time
import uuid

from common.type_def import LifecycleState, MemoryTier, MemoryUnit, Segment
from construction.base import OperatorType
from construction.extractor import Extractor, ExtractorProducer

WIKIMEM_ACTION = "wikimem.action"
WIKIMEM_KEY = "wikimem.key"
WIKIMEM_VALUE = "wikimem.value"
WIKIMEM_MEMORY_TYPE = "wikimem.memory_type"
WIKIMEM_PREFERRED_SCOPE = "wikimem.preferred_scope"
WIKIMEM_SOURCE_MESSAGE_ID = "wikimem.source_message_id"
WIKIMEM_OBSERVED_AT_MS = "wikimem.observed_at_ms"
WIKIMEM_SCORE = "wikimem.score"
WIKIMEM_SKIP_REASON = "wikimem.skip_reason"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_INTERROGATIVE_KEYS = {"who", "what", "when", "where", "why", "how", "which"}
_SECRET_MARKERS = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "sk-ant-",
    "sk-proj-",
    "sk-svcacct-",
    "sk-admin-",
    "xoxb-",
    "xoxp-",
    "xoxe-",
    "xapp-",
    "akia",
    "asia",
    "abia",
    "acca",
)
_SCOPE_PREFIXES = (
    ("this just for me", "auto"),
    ("just for me", "auto"),
    ("this for me only", "auto"),
    ("for me only", "auto"),
    ("this from private memory", "auto"),
    ("from private memory", "auto"),
    ("in private memory", "auto"),
    ("to private memory", "auto"),
    ("for private memory", "auto"),
    ("this in auto memory", "auto"),
    ("in auto memory", "auto"),
    ("to auto memory", "auto"),
    ("for auto memory", "auto"),
    ("this for the team", "team"),
    ("for the team", "team"),
    ("this from team memory", "team"),
    ("from team memory", "team"),
    ("in team memory", "team"),
    ("to team memory", "team"),
    ("for team memory", "team"),
    ("只给我", "auto"),
    ("私有记忆里", "auto"),
    ("私有记忆", "auto"),
    ("私人记忆里", "auto"),
    ("私人记忆", "auto"),
    ("团队记忆里", "team"),
    ("团队记忆", "team"),
    ("团队共享", "team"),
    ("给团队", "team"),
)


class WikimemBaselineExtractor(Extractor):
    """Extract wikimem baseline candidates from raw MemoryUnit content."""

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        candidates: list[MemoryUnit] = []
        for unit in units:
            if unit.lifecycle != LifecycleState.ACTIVE or unit.provenance:
                continue
            parsed = parse_memory_candidate(unit.content, unit.source_ref or unit.id)
            if parsed is None:
                continue
            candidates.append(_candidate_unit(unit, parsed))
        return candidates


def parse_memory_candidate(text: str, source_message_id: str) -> dict[str, str] | None:
    """Parse one wikimem baseline candidate from text."""

    stripped = text.strip()
    if not stripped:
        return None
    return (
        _parse_explicit_forget_instruction(stripped, source_message_id)
        or _parse_explicit_memory_instruction(stripped, source_message_id)
        or _parse_key_value_candidate(stripped, source_message_id)
    )


def _parse_key_value_candidate(text: str, source_message_id: str) -> dict[str, str] | None:
    for separator in (":", " is ", "="):
        if separator not in text:
            continue
        key, value = text.split(separator, 1)
        key = key.strip()
        value = value.strip()
        if not key or not value or key.lower() in _INTERROGATIVE_KEYS:
            continue
        memory_type = _infer_memory_type(key, value)
        return _candidate_metadata(
            action="upsert",
            key=key,
            value=value,
            source_message_id=source_message_id,
            memory_type=memory_type,
            preferred_scope="",
        )
    return None


def _parse_explicit_memory_instruction(
    text: str, source_message_id: str
) -> dict[str, str] | None:
    prefixes = (
        "please remember that ",
        "please remember ",
        "remember that ",
        "remember ",
    )
    lower = text.lower()
    raw_value = ""
    for prefix in prefixes:
        if lower.startswith(prefix):
            raw_value = text[len(prefix):].strip()
            break
    if not raw_value:
        if text.startswith("请记住"):
            raw_value = text[len("请记住"):].strip()
        elif text.startswith("记住"):
            raw_value = text[len("记住"):].strip()
        else:
            return None

    preferred_scope, value = _extract_scope_hint(raw_value)
    value = _clean_instruction_value(value)
    if len(value) < 8:
        return None
    return _candidate_metadata(
        action="upsert",
        key=_derive_memory_note_key(value),
        value=value,
        source_message_id=source_message_id,
        memory_type=_infer_memory_type("memory", value),
        preferred_scope=preferred_scope,
    )


def _parse_explicit_forget_instruction(
    text: str, source_message_id: str
) -> dict[str, str] | None:
    prefixes = (
        "please forget that ",
        "please forget ",
        "forget that ",
        "forget ",
    )
    lower = text.lower()
    raw_target = ""
    for prefix in prefixes:
        if lower.startswith(prefix):
            raw_target = text[len(prefix):].strip()
            break
    if not raw_target:
        if text.startswith("请忘记"):
            raw_target = text[len("请忘记"):].strip()
        elif text.startswith("忘记"):
            raw_target = text[len("忘记"):].strip()
        else:
            return None

    preferred_scope, target = _extract_scope_hint(raw_target)
    target = _clean_instruction_value(target)
    target = _strip_forget_article(target)
    if not target:
        return None
    key = _derive_memory_note_key(target) if " " in target else target
    return _candidate_metadata(
        action="forget",
        key=key,
        value=target,
        source_message_id=source_message_id,
        memory_type="",
        preferred_scope=preferred_scope,
    )


def _candidate_metadata(
    *,
    action: str,
    key: str,
    value: str,
    source_message_id: str,
    memory_type: str,
    preferred_scope: str,
) -> dict[str, str]:
    metadata = {
        WIKIMEM_ACTION: action,
        WIKIMEM_KEY: key,
        WIKIMEM_VALUE: value,
        WIKIMEM_SOURCE_MESSAGE_ID: source_message_id,
        WIKIMEM_OBSERVED_AT_MS: str(int(time.time() * 1000)),
    }
    if preferred_scope:
        metadata[WIKIMEM_PREFERRED_SCOPE] = preferred_scope
    if memory_type:
        metadata[WIKIMEM_MEMORY_TYPE] = memory_type
        metadata[WIKIMEM_SCORE] = str(_candidate_score(memory_type))
    if preferred_scope == "team" and action == "upsert" and _has_potential_secret(value):
        metadata[WIKIMEM_SKIP_REASON] = "potential_secret"
    return metadata


def _candidate_unit(source: MemoryUnit, metadata: dict[str, str]) -> MemoryUnit:
    return MemoryUnit(
        id=f"wikimem_candidate_{uuid.uuid4().hex}",
        scope=source.scope,
        tier=MemoryTier.SEMANTIC,
        segments=[Segment(content=metadata[WIKIMEM_VALUE], source=source.source)],
        source_ref=metadata[WIKIMEM_SOURCE_MESSAGE_ID],
        temporal=source.temporal,
        provenance=[source.id],
        tags=[*source.tags, "wikimem_candidate"],
        metadata=metadata,
        lifecycle=LifecycleState.ACTIVE,
    )


def _extract_scope_hint(text: str) -> tuple[str, str]:
    trimmed = text.strip()
    lower = trimmed.lower()
    for prefix, scope in _SCOPE_PREFIXES:
        if lower.startswith(prefix):
            return scope, trimmed[len(prefix):].lstrip(":, \t").strip()
    return "", trimmed


def _clean_instruction_value(value: str) -> str:
    return value.strip(":, \t").rstrip(".!?。！？").strip()


def _strip_forget_article(target: str) -> str:
    lower = target.lower()
    for prefix in ("the ", "this "):
        if lower.startswith(prefix):
            return target[len(prefix):].strip()
    return target


def _derive_memory_note_key(value: str) -> str:
    tokens = _ordered_tokens(value)[:4]
    if not tokens:
        return "memory_note"
    return f"memory_note_{'_'.join(tokens)}"


def _ordered_tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text) if len(match.group(0)) > 1]


def _infer_memory_type(key: str, value: str) -> str:
    normalized_key = key.lower()
    normalized_value = value.lower()
    if normalized_key in {"user", "role", "experience", "knowledge"}:
        return "user"
    feedback_key = normalized_key in {"feedback", "preference", "preferences", "rule"}
    feedback_language = any(
        marker in normalized_value for marker in ("prefer", "don't", "must")
    )
    if feedback_key or feedback_language:
        return "feedback"
    reference_key = normalized_key in {
        "reference",
        "dashboard",
        "linear",
        "slack",
        "grafana",
        "url",
    }
    reference_language = any(
        marker in normalized_value for marker in ("http", "grafana", "linear", "slack")
    )
    if reference_key or reference_language:
        return "reference"
    return "project"


def _candidate_score(memory_type: str) -> float:
    if memory_type == "feedback":
        return 1.25
    if memory_type == "reference":
        return 1.15
    if memory_type == "user":
        return 1.1
    return 1.0


def _has_potential_secret(value: str) -> bool:
    lower = value.lower()
    if "-----begin" in lower and "private key-----" in lower:
        return True
    return any(marker in lower for marker in _SECRET_MARKERS)


@ExtractorProducer.register("wikimem_baseline")
def _build(config):
    return WikimemBaselineExtractor()
