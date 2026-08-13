"""两个 MemoryEngine 实现共用的 List 存储委托。"""

from __future__ import annotations

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import FilterExpr, Scope
from jiuwen_memory.control.types import MemoryListResult
from jiuwen_memory.storage.storage import Storage


def list_page(
    storage: Storage,
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
    stored = storage.list(
        scope,
        offset=offset,
        limit=limit,
        memory_types=memory_types,
        filters=filters,
        extensions=extensions,
    )
    return MemoryListResult(items=stored.items, count=stored.count)
