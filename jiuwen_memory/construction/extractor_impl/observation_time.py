# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-source date context shared by legacy and dynamic extraction."""

from datetime import UTC, datetime

from jiuwen_memory.common.type_def import MemoryUnit
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


def observation_sources(
    units: list[MemoryUnit], source_prefix: str,
    *, date_prefix: str = "observation_date: {date}\n",
) -> str:
    return "\n".join(
        date_prefix.format(date=observation_date(unit))
        + source_prefix.format(
            unit_id=unit.id,
            unit_content=truncate_unit_content(unit.content, limit=CONTENT_MAX_UNIT_CHARS),
        )
        for unit in units
    )
