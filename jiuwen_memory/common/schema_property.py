# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Canonical identity and admission rules for Schema Property MemoryUnits."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .type_def import MemoryUnit


@dataclass(frozen=True, slots=True)
class SchemaPropertyIdentity:
    """Canonical owner information persisted by the derived reverse index."""

    entity_key: str
    entity_name: str
    entity_type: str


def schema_entity_fallback_key(entity_type: object, entity_name: object) -> str:
    """Build the deterministic owner used by legacy properties without an id."""

    type_part = _normalize_identity_part(entity_type) or "entity"
    name_part = _normalize_identity_part(entity_name)
    return f"{type_part}::{name_part}" if name_part else ""


def schema_property_entity_id(unit: MemoryUnit) -> str:
    """Return one Property owner's canonical id, including the legacy fallback."""

    metadata = unit.system_metadata
    explicit = _string(
        metadata.get("schema_entity_id") or metadata.get("schema_entity_key")
    ).strip()
    if explicit:
        return explicit
    return schema_entity_fallback_key(
        metadata.get("schema_entity_type"),
        metadata.get("schema_entity_name"),
    )


def is_schema_property_unit(unit: MemoryUnit) -> bool:
    """Apply the single write/read admission predicate for Schema Properties."""

    metadata = unit.system_metadata
    operation = _string(metadata.get("schema_property_operation") or "set")
    return bool(
        metadata.get("extraction_mode") == "schema"
        and _string(metadata.get("schema_property_name")).strip()
        and schema_property_entity_id(unit)
        and operation.strip().casefold() != "delete"
    )


def schema_property_identity(unit: MemoryUnit) -> SchemaPropertyIdentity | None:
    """Return the canonical index identity when ``unit`` is an admitted Property."""

    if not is_schema_property_unit(unit):
        return None
    metadata = unit.system_metadata
    return SchemaPropertyIdentity(
        entity_key=schema_property_entity_id(unit),
        entity_name=_string(metadata.get("schema_entity_name")).strip(),
        entity_type=_string(metadata.get("schema_entity_type")).strip(),
    )


def _normalize_identity_part(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", _string(value)).strip()
    return " ".join(normalized.split())


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
