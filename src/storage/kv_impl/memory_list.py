"""KVStore MemoryUnit 列表查询的公共兼容实现。"""

from __future__ import annotations

from datetime import datetime, timezone

from common.type_def import FilterExpr, MemoryUnit, matches_memory_unit
from common.type_def.memory_codec import loads

from ..types import KVMemoryListResult


def _memory_type(unit: MemoryUnit) -> str:
    return str(unit.metadata.get("memory_type", "")).strip() or unit.tier.value


def _sort_key(unit: MemoryUnit) -> tuple[datetime, str]:
    ingested_at = unit.temporal.t_ingest or datetime.min.replace(tzinfo=timezone.utc)
    return ingested_at, unit.id


def list_memory_entries(
    entries: list[tuple[str, bytes]],
    *,
    offset: int,
    limit: int,
    memory_types: list[str] | None,
    filters: FilterExpr | None,
    extensions: dict[str, str] | None,
) -> KVMemoryListResult:
    """过滤、计数、稳定排序并分页；未知 extensions 原样接受但不解释。"""
    _ = extensions
    wanted: set[str] = set()
    for raw_memory_type in memory_types or ():
        memory_type = str(raw_memory_type).strip()
        if memory_type:
            wanted.add(memory_type)
    matches: list[tuple[str, bytes, MemoryUnit]] = []
    for key, raw in entries:
        unit = loads(raw)
        if unit is None:
            continue
        if wanted and _memory_type(unit) not in wanted:
            continue
        if not matches_memory_unit(unit, filters):
            continue
        matches.append((key, raw, unit))
    matches.sort(key=lambda item: _sort_key(item[2]), reverse=True)
    count = len(matches)
    page = matches[offset : offset + limit]
    return KVMemoryListResult(
        entries=[(key, raw) for key, raw, _ in page],
        count=count,
    )
