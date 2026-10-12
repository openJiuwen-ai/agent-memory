# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build TemporalEntity views from entity records and schema property units."""

from __future__ import annotations

from jiuwen_memory.common.type_def import EntityRecord, MemoryUnit

from .model import SchemaPropertyEntry, TemporalEntity


class TemporalEntityAssembler:
    """Assemble one read-only entity view without creating another truth source."""

    def assemble(
        self,
        entity_id: str,
        property_units: list[MemoryUnit],
        *,
        entity_record: MemoryUnit | EntityRecord | None = None,
        truncated: bool = False,
    ) -> TemporalEntity:
        representative = next(iter(property_units), None)
        metadata = representative.system_metadata if representative is not None else {}
        name = str(metadata.get("schema_entity_name") or "").strip()
        entity_type = str(metadata.get("schema_entity_type") or "").strip()
        description = str(metadata.get("schema_entity_description") or "").strip()
        aliases = _string_list(metadata.get("schema_entity_aliases"))

        if isinstance(entity_record, MemoryUnit):
            record_metadata = entity_record.system_metadata
            name = str(record_metadata.get("schema_entity_name") or name).strip()
            entity_type = str(record_metadata.get("schema_entity_type") or entity_type).strip()
            description = entity_record.content or description
            aliases = _string_list(record_metadata.get("schema_entity_aliases")) or aliases
        elif isinstance(entity_record, EntityRecord):
            name = entity_record.entity_text or name
            entity_type = entity_record.entity_type or entity_type

        entity = TemporalEntity(
            entity_id=entity_id,
            name=name,
            entity_type=entity_type,
            description=description,
            aliases=aliases,
            search_fields=_search_fields(entity_record, representative),
            truncated=truncated,
        )
        for unit in property_units:
            if unit.system_metadata.get("extraction_mode") != "schema":
                continue
            entry = SchemaPropertyEntry.from_unit(unit)
            if entry.entity_id == entity_id and entry.property_name:
                entity.add(entry)
        return entity


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _search_fields(
    entity_record: MemoryUnit | EntityRecord | None,
    representative: MemoryUnit | None,
) -> list[str]:
    values: list[str] = []
    if representative is not None:
        metadata = representative.system_metadata
        values.extend(_string_list(metadata.get("schema_entity_search_fields")))
        values.append(str(metadata.get("schema_entity_name") or "").strip())
        values.extend(_string_list(metadata.get("schema_entity_aliases")))
        values.append(str(metadata.get("schema_entity_type") or "").strip())
        values.append(representative.content.strip())
    if isinstance(entity_record, MemoryUnit):
        values.append(entity_record.content.strip())
    elif isinstance(entity_record, EntityRecord):
        values.append(entity_record.entity_text.strip())
        values.append(entity_record.entity_type.strip())
    return list(dict.fromkeys(value for value in values if value))
