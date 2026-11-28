# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-source date context shared by legacy and dynamic extraction."""

from datetime import UTC, datetime

from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.construction.message_role import fence_role_content, message_role_of
from jiuwen_memory.construction.prompt_guard import CONTENT_MAX_UNIT_CHARS, truncate_unit_content


def observation_date(unit: MemoryUnit) -> str:
    explicit = str(unit.system_metadata.get("observation_date") or "").strip()
    if explicit:
        return explicit.split("T", 1)[0]
    message_time = unit.temporal.t_message
    if message_time is not None and message_time.utcoffset() is not None:
        # Preserve the source's calendar day, including its timezone.
        return message_time.date().isoformat()
    return datetime.now(UTC).date().isoformat()


def _format_unit_body(unit: MemoryUnit) -> str:
    truncated = truncate_unit_content(unit.content, limit=CONTENT_MAX_UNIT_CHARS)
    role = message_role_of(unit)
    if not role:
        return truncated
    return f"[ROLE: {role}]\n{fence_role_content(truncated)}"


def observation_sources(
    units: list[MemoryUnit], source_prefix: str,
    *, date_prefix: str = "observation_date: {date}\n",
    labels: list[str] | None = None,
) -> str:
    unit_labels = labels if labels is not None else [unit.id for unit in units]
    return "\n".join(
        date_prefix.format(date=observation_date(unit))
        + source_prefix.format(
            unit_id=label,
            unit_content=_format_unit_body(unit),
        )
        for unit, label in zip(units, unit_labels, strict=True)
    )
