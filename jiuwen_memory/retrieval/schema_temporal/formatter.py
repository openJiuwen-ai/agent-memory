# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prompt-oriented formatting of temporal schema entities."""

from __future__ import annotations

from .model import EventTimePrecision, SchemaPropertyEntry, TemporalEntity


class SchemaTemporalFormatter:
    """Render each entity independently while preserving time precision."""

    def format_entity(self, entity: TemporalEntity) -> str:
        lines = [f"Entity: {entity.name or entity.entity_id}"]
        if entity.entity_type:
            lines.append(f"Type: {entity.entity_type}")
        if entity.description:
            lines.append(f"Description: {entity.description}")
        for name, timeline in entity.properties.items():
            lines.extend(("", f"{name}:"))
            for entry in timeline:
                lines.append(f"- {_format_time(entry)}: {entry.value}")
        if entity.fallback_used:
            lines.append("[Temporal fallback: full timeline shown]")
        if entity.truncated:
            lines.append("[Timeline truncated]")
        return "\n".join(lines)

    def format_entities(
        self,
        entities: list[TemporalEntity],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Format every entity with a per-entity, never global, character bound."""

        blocks: list[str] = []
        for entity in entities:
            block = self.format_entity(entity)
            if max_chars is not None and len(block) > max_chars:
                block = block[:max_chars]
            blocks.append(block)
        return "\n\n".join(blocks)


def _format_time(entry: SchemaPropertyEntry) -> str:
    value = entry.event_start or entry.event_time
    if value is None:
        if entry.message_time is None:
            return "undated"
        return f"event time not stated; source message {entry.message_time.date().isoformat()}"
    if entry.event_precision is EventTimePrecision.YEAR:
        return f"{value.year:04d}"
    if entry.event_precision is EventTimePrecision.MONTH:
        return f"{value.year:04d}-{value.month:02d}"
    if entry.event_precision is EventTimePrecision.DAY:
        return value.date().isoformat()
    return value.isoformat()
