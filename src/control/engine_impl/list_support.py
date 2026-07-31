"""两个 MemoryEngine 实现共用的 List 存储委托。"""

from __future__ import annotations

from common.errors import ValidationError
from common.type_def import FilterExpr, Scope
from common.type_def.memory_codec import loads
from control.types import MemoryListResult
from storage.kv import KVStore


def list_page(
    kv: KVStore,
    scope: Scope,
    *,
    offset: int,
    limit: int,
    memory_types: list[str] | None,
    filters: FilterExpr | None,
    extensions: dict[str, str] | None,
) -> MemoryListResult:
    """校验分页参数，透传查询并只反序列化当前页。"""
    if offset < 0:
        raise ValidationError("offset must be >= 0")
    if limit <= 0:
        raise ValidationError("limit must be > 0")
    stored = kv.list(
        scope,
        offset=offset,
        limit=limit,
        memory_types=memory_types,
        filters=filters,
        extensions=extensions,
    )
    items = []
    for _, raw in stored.entries:
        unit = loads(raw)
        if unit is not None:
            items.append(unit)
    return MemoryListResult(items=items, count=stored.count)
